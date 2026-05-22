import asyncio
import json
import os
import uuid as _uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi_users.authentication import JWTStrategy
import redis.asyncio as redis

from server.auth import _authenticate_slave_api_key
from server.auth import COOKIE_NAME, JWT_SECRET, COOKIE_MAX_AGE

info_exchange_router = APIRouter()

REDIS_URL            = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
# Every subscriber's worker listens here; publisher's worker publishes here.
RTC_PUB_CHANNEL      = "rtc:to_publisher"
# Publisher's worker publishes here; each subscriber's worker listens on its own channel.
RTC_SUB_PREFIX       = "rtc:to_sub:"
# Durable SET of subscriber peer_ids currently waiting for an offer.
# Survives Redis pub/sub fire-and-forget so a late publisher can catch up.
REDIS_PENDING_SUBS   = "rtc:pending_subs"
REDIS_PENDING_SUB_TTL = 3600  # seconds; auto-expire stale entries after 1 h

_redis_client: redis.Redis | None = None

# In-process fallback state (single-worker / dev without Redis)
_rtc_publisher: dict | None = None
_rtc_subscribers: dict[str, WebSocket] = {}
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

# ── Redis relay helpers (multi-worker safe) ──

async def _signal_redis_publisher(websocket: WebSocket, peer_id: str) -> None:
    """Publisher side: subscribe to RTC_PUB_CHANNEL so messages from subscribers
    on any worker reach this WebSocket; relay outbound offers to per-subscriber channels."""
    async with _redis_client.pubsub() as pubsub:
        await pubsub.subscribe(RTC_PUB_CHANNEL)

        # Catch any subscribers that arrived before the publisher connected.
        # They registered in REDIS_PENDING_SUBS; re-send subscriber_ready for each.
        try:
            pending = await _redis_client.smembers(REDIS_PENDING_SUBS)
            for sub_id in pending:
                if isinstance(sub_id, bytes):
                    sub_id = sub_id.decode("utf-8")
                try:
                    await websocket.send_text(
                        json.dumps({"type": "subscriber_ready", "sub_id": sub_id})
                    )
                except Exception:
                    break
        except Exception:
            pass

        async def _redis_to_ws():
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    await websocket.send_text(data)
                except Exception:
                    return

        relay = asyncio.create_task(_redis_to_ws())
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                msg["from"] = peer_id
                target_id = msg.get("target")
                if target_id:
                    await _redis_client.publish(
                        f"{RTC_SUB_PREFIX}{target_id}", json.dumps(msg)
                    )
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            relay.cancel()
            try:
                await relay
            except asyncio.CancelledError:
                pass


async def _signal_redis_subscriber(websocket: WebSocket, peer_id: str) -> None:
    """Subscriber side: subscribe to its own per-id channel so offers from the
    publisher on any worker reach this WebSocket; relay answers back to the publisher."""
    async with _redis_client.pubsub() as pubsub:
        await pubsub.subscribe(f"{RTC_SUB_PREFIX}{peer_id}")

        # Register in the durable pending set BEFORE publishing subscriber_ready so
        # a publisher that connects later will find this subscriber in the set.
        try:
            await _redis_client.sadd(REDIS_PENDING_SUBS, peer_id)
            await _redis_client.expire(REDIS_PENDING_SUBS, REDIS_PENDING_SUB_TTL)
        except Exception:
            pass

        # Tell publisher (on whatever worker it landed on) a new subscriber is ready.
        await _redis_client.publish(
            RTC_PUB_CHANNEL,
            json.dumps({"type": "subscriber_ready", "sub_id": peer_id}),
        )

        async def _redis_to_ws():
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    await websocket.send_text(data)
                except Exception:
                    return

        relay = asyncio.create_task(_redis_to_ws())
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                msg["from"] = peer_id
                await _redis_client.publish(RTC_PUB_CHANNEL, json.dumps(msg))
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            relay.cancel()
            try:
                await relay
            except asyncio.CancelledError:
                pass
            # Remove from pending set so the publisher won't re-offer a dead subscriber.
            try:
                await _redis_client.srem(REDIS_PENDING_SUBS, peer_id)
            except Exception:
                pass
            try:
                await _redis_client.publish(
                    RTC_PUB_CHANNEL,
                    json.dumps({"type": "subscriber_left", "sub_id": peer_id}),
                )
            except Exception:
                pass


# ── In-process fallback helpers (single-worker / dev without Redis) ──

async def _signal_local_publisher(websocket: WebSocket, peer_id: str) -> None:
    global _rtc_publisher
    async with _rtc_lock:
        _rtc_publisher = {"ws": websocket, "id": peer_id}
        existing_subs = list(_rtc_subscribers.keys())

    for sub_id in existing_subs:
        try:
            await websocket.send_text(
                json.dumps({"type": "subscriber_ready", "sub_id": sub_id})
            )
        except Exception:
            break

    try:
        while True:
            try:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                msg["from"] = peer_id
                async with _rtc_lock:
                    target_ws = _rtc_subscribers.get(msg.get("target"))
                if target_ws:
                    try:
                        await target_ws.send_text(json.dumps(msg))
                    except Exception:
                        pass
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        async with _rtc_lock:
            if _rtc_publisher and _rtc_publisher["id"] == peer_id:
                _rtc_publisher = None


async def _signal_local_subscriber(websocket: WebSocket, peer_id: str) -> None:
    async with _rtc_lock:
        _rtc_subscribers[peer_id] = websocket
        pub = _rtc_publisher

    if pub:
        try:
            await pub["ws"].send_text(
                json.dumps({"type": "subscriber_ready", "sub_id": peer_id})
            )
        except Exception:
            pass

    try:
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
            _rtc_subscribers.pop(peer_id, None)
            pub = _rtc_publisher
        if pub:
            try:
                await pub["ws"].send_text(
                    json.dumps({"type": "subscriber_left", "sub_id": peer_id})
                )
            except Exception:
                pass


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

    if _redis_client is not None:
        if role == "publisher":
            await _signal_redis_publisher(websocket, peer_id)
        else:
            await _signal_redis_subscriber(websocket, peer_id)
    else:
        if role == "publisher":
            await _signal_local_publisher(websocket, peer_id)
        else:
            await _signal_local_subscriber(websocket, peer_id)


async def info_exchange_startup() -> None:
    """Connect to Redis. Falls back to in-process mode if Redis is unavailable."""
    global _redis_client
    try:
        client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
        await client.ping()
        _redis_client = client
    except Exception:
        _redis_client = None


async def info_exchange_shutdown() -> None:
    """Close the Redis connection cleanly."""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None

