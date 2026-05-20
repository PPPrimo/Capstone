import asyncio
import json
import uuid as _uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi_users.authentication import JWTStrategy

from server.auth import _authenticate_slave_api_key
from server.auth import COOKIE_NAME, JWT_SECRET, COOKIE_MAX_AGE

info_exchange_router = APIRouter()

# ── WebRTC signaling state ──
_rtc_publisher: dict | None = None           # {"ws": WebSocket, "id": str}
_rtc_subscribers: dict[str, WebSocket] = {}  # peer_id -> WebSocket
_rtc_lock = asyncio.Lock()


# ── Helper: authenticate a WebSocket connection via cookie JWT or API key ──
async def _ws_authenticate(websocket: WebSocket) -> bool:
    """Validate the browser cookie JWT or X-API-Key query param on a WebSocket upgrade.
    Returns True if authenticated, False otherwise."""

    # 1) Try API key from query param: ?api_key=...
    api_key = websocket.query_params.get("api_key")
    if api_key:
        try:
            from server.db import async_session_maker
            async with async_session_maker() as session:
                user = await _authenticate_slave_api_key(api_key, session)
                if user is not None and getattr(user, "is_active", False):
                    return True
        except Exception:
            pass

    # 2) Try browser cookie JWT
    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        strategy = JWTStrategy(secret=JWT_SECRET, lifetime_seconds=COOKIE_MAX_AGE)
        from server.db import async_session_maker
        async with async_session_maker() as session:
            from server.models import User as UserModel
            from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
            user_db = SQLAlchemyUserDatabase(session, UserModel)
            from server.auth import UserManager
            user_manager = UserManager(user_db)
            user = await strategy.read_token(token, user_manager)
            return user is not None and getattr(user, "is_active", False)
    except Exception:
        return False

# ── WebRTC signaling endpoint ──

@info_exchange_router.websocket("/api/webrtc/signal")
async def webrtc_signal(websocket: WebSocket):
    """Relay SDP offers/answers and ICE candidates between the robot publisher
    and browser subscribers so they can establish a WebRTC DataChannel.

    Publisher auth : ?role=publisher&api_key=<SLAVE_API_KEY>
    Subscriber auth: ?role=subscriber  (browser cookie JWT)
    """
    role    = websocket.query_params.get("role", "")
    peer_id = str(_uuid.uuid4())

    # --- Authenticate before accepting ---
    if role == "publisher":
        api_key = websocket.query_params.get("api_key")
        try:
            from server.db import async_session_maker
            async with async_session_maker() as session:
                user = await _authenticate_slave_api_key(api_key, session)
                if not user or not getattr(user, "is_active", False):
                    await websocket.close(code=4401, reason="Unauthorized")
                    return
        except Exception:
            await websocket.close(code=4401, reason="Unauthorized")
            return
    elif role == "subscriber":
        if not await _ws_authenticate(websocket):
            await websocket.close(code=4401, reason="Unauthorized")
            return
    else:
        await websocket.close(code=4400, reason="Bad role")
        return

    await websocket.accept()
    global _rtc_publisher, _rtc_subscribers

    try:
        if role == "publisher":
            async with _rtc_lock:
                _rtc_publisher = {"ws": websocket, "id": peer_id}
                existing_subs = list(_rtc_subscribers.keys())

            # Notify about any subscribers that were waiting before publisher arrived
            for sub_id in existing_subs:
                try:
                    await websocket.send_text(
                        json.dumps({"type": "subscriber_ready", "sub_id": sub_id})
                    )
                except Exception:
                    break

            # Relay loop: publisher → target subscriber
            while True:
                try:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    msg["from"] = peer_id
                    target_id = msg.get("target")
                    async with _rtc_lock:
                        target_ws = _rtc_subscribers.get(target_id)
                    if target_ws:
                        try:
                            await target_ws.send_text(json.dumps(msg))
                        except Exception:
                            pass
                except WebSocketDisconnect:
                    break
                except Exception:
                    break

        else:  # subscriber
            async with _rtc_lock:
                _rtc_subscribers[peer_id] = websocket
                pub = _rtc_publisher

            # Notify publisher so it can initiate a new PeerConnection for this subscriber
            if pub:
                try:
                    await pub["ws"].send_text(
                        json.dumps({"type": "subscriber_ready", "sub_id": peer_id})
                    )
                except Exception:
                    pass

            # Relay loop: subscriber → publisher
            while True:
                try:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    msg["from"] = peer_id
                    async with _rtc_lock:
                        pub = _rtc_publisher
                    if pub:
                        try:
                            await pub["ws"].send_text(json.dumps(msg))
                        except Exception:
                            pass
                except WebSocketDisconnect:
                    break
                except Exception:
                    break

    finally:
        async with _rtc_lock:
            if role == "publisher":
                if _rtc_publisher and _rtc_publisher["id"] == peer_id:
                    _rtc_publisher = None
            else:
                _rtc_subscribers.pop(peer_id, None)
                pub = _rtc_publisher
        # Tell the publisher to tear down this subscriber's PeerConnection
        if role == "subscriber" and pub:
            try:
                await pub["ws"].send_text(
                    json.dumps({"type": "subscriber_left", "sub_id": peer_id})
                )
            except Exception:
                pass

