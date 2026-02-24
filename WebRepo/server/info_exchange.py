import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, Cookie, Query
from fastapi.responses import PlainTextResponse, StreamingResponse

from server.models import User
from server.auth import _require_active_user_or_api_key, get_async_session, _authenticate_slave_api_key
from server.auth import current_optional_user, COOKIE_NAME, JWT_SECRET, COOKIE_MAX_AGE

from fastapi_users.authentication import JWTStrategy
from sqlalchemy import select

info_exchange_router = APIRouter()

_latest_payload: dict | None = None
_latest_received_at: float | None = None
_latest_publisher: str | None = None

# WebSocket subscribers receive *latest-only* updates.
# Each client has its own queue (maxsize=1) so slow clients never build backlog
# and cannot introduce lag/jitter for others.
_subscribers: set[asyncio.Queue[str]] = set()
_subscribers_lock = asyncio.Lock()

# ── Helper: authenticate a WebSocket connection via cookie JWT or API key ──
async def _ws_authenticate(websocket: WebSocket) -> bool:
    """Validate the browser cookie JWT or X-API-Key query param on a WebSocket upgrade.
    Returns True if authenticated, False otherwise."""

    # 1) Try API key from query param: /api/ws?api_key=...
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

#Responds to follower request (*Outdated)
@info_exchange_router.get("/api/latest")
async def latest(_: User = Depends(_require_active_user_or_api_key)):
    if _latest_payload is None:
        return {"received_at": None, "payload": None}
    return {"received_at": _latest_received_at, "publisher": _latest_publisher, "payload": _latest_payload}

#Recieves leader action *needs to modify into dual direction
@info_exchange_router.post("/api/ingest")
async def ingest(payload: dict, request: Request, session=Depends(get_async_session)):
    """API for the slave node to publish the latest telemetry snapshot.

    Auth: `X-API-Key: <SLAVE_API_KEY>`.
    """
    api_key = request.headers.get("x-api-key")
    user = await _authenticate_slave_api_key(api_key, session)
    if not user:
        return PlainTextResponse("Unauthorized", status_code=401)

    global _latest_payload, _latest_received_at, _latest_publisher
    _latest_payload = payload
    _latest_received_at = time.time()
    _latest_publisher = user.email

    message = json.dumps(
        {
            "received_at": _latest_received_at,
            "publisher": user.email,
            "payload": _latest_payload,
        },
        separators=(",", ":"),
    )

    async with _subscribers_lock:
        for q in list(_subscribers):
            # Latest-only: if the client is slow, drop the older pending item.
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

    return {"ok": True}

@info_exchange_router.websocket("/api/ws")
async def ws_stream(websocket: WebSocket):
    """WebSocket endpoint for real-time telemetry push to the browser."""

    # Authenticate via cookie before accepting
    if not await _ws_authenticate(websocket):
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept()

    # Heuristic:
    # - API-key clients are teleop (need full-rate, minimal jitter)
    # - Cookie-auth clients are browser UI (throttle to reduce impact on teleop)
    is_ui_client = websocket.query_params.get("api_key") is None

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    async with _subscribers_lock:
        _subscribers.add(queue)

    PING_INTERVAL_S = 1.0
    SEND_TIMEOUT_S = 1.0
    UI_MIN_INTERVAL_S = 0.2  # 5Hz; UI doesn't need 100ms updates
    last_ui_send_at = 0.0

    try:
        # Always send the latest snapshot so new clients have data immediately.
        if _latest_payload is not None:
            initial = json.dumps(
                {"received_at": _latest_received_at, "publisher": _latest_publisher, "payload": _latest_payload},
                separators=(",", ":"),
            )
            await asyncio.wait_for(websocket.send_text(initial), timeout=SEND_TIMEOUT_S)

        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL_S)

                if is_ui_client:
                    now = time.monotonic()
                    if now - last_ui_send_at < UI_MIN_INTERVAL_S:
                        continue  # drop intermediate UI updates; keep newest-only behavior
                    last_ui_send_at = now

                await asyncio.wait_for(websocket.send_text(msg), timeout=SEND_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Keepalive ping (helps traverse Cloudflare and detects half-open sockets quickly).
                await asyncio.wait_for(websocket.send_text('{"ping":true}'), timeout=SEND_TIMEOUT_S)
            except WebSocketDisconnect:
                break
            except asyncio.CancelledError:
                break
            except Exception:
                break
    finally:
        async with _subscribers_lock:
            _subscribers.discard(queue)