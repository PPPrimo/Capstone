import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, Cookie, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi_users.authentication import JWTStrategy
from sqlalchemy import select
import redis.asyncio as redis

from server.models import User
from server.auth import _require_active_user_or_api_key, get_async_session, _authenticate_slave_api_key
from server.auth import current_optional_user, COOKIE_NAME, JWT_SECRET, COOKIE_MAX_AGE

info_exchange_router = APIRouter()

REDIS_URL        = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
REDIS_LATEST_KEY = "info_exchange:latest"
REDIS_CHANNEL    = "info_exchange:update"

# Per-worker local cache — updated by the Redis subscriber task so the WS
# initial-snapshot requires no extra Redis GET.  Also used as fallback when
# Redis is unavailable (single-worker dev mode).
_local_cache: str | None = None

_redis_client: redis.Redis | None = None
_redis_sub_task: asyncio.Task | None = None

# Per-worker WebSocket subscribers — asyncio queues for intra-process fan-out.
# Each client has its own queue (maxsize=1) so slow clients never build backlog.
_subscribers: set[asyncio.Queue[str]] = set()
_subscribers_lock = asyncio.Lock()


async def _fan_out_local(message: str) -> None:
    """Push a message to all local (intra-worker) WebSocket subscriber queues."""
    async with _subscribers_lock:
        for q in list(_subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass


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
    raw: str | None = None
    if _redis_client is not None:
        try:
            raw = await _redis_client.get(REDIS_LATEST_KEY)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
        except Exception:
            pass
    if raw is None:
        raw = _local_cache
    if raw is None:
        return {"received_at": None, "payload": None}
    return json.loads(raw)

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

    global _local_cache
    received_at = time.time()
    message = json.dumps(
        {
            "received_at": received_at,
            "publisher": user.email,
            "payload": payload,
        },
        separators=(",", ":"),
    )

    if _redis_client is not None:
        try:
            await _redis_client.set(REDIS_LATEST_KEY, message)
            await _redis_client.publish(REDIS_CHANNEL, message)
            # Fan-out to local WS queues is handled by _redis_fan_out_task
            # running in every worker — do NOT also call _fan_out_local here
            # or subscribers in this worker would receive the message twice.
        except Exception:
            # Redis unavailable mid-operation — fall back to local delivery.
            _local_cache = message
            await _fan_out_local(message)
    else:
        # No Redis: single-worker in-process fallback.
        _local_cache = message
        await _fan_out_local(message)

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
        initial: str | None = None
        if _redis_client is not None:
            try:
                raw = await _redis_client.get(REDIS_LATEST_KEY)
                if raw is not None:
                    initial = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            except Exception:
                pass
        if initial is None:
            initial = _local_cache
        if initial is not None:
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


# ── Redis pub/sub subscriber: one task per worker process ──

async def _redis_fan_out_task() -> None:
    """Subscribe to the Redis telemetry channel and fan out to local WS queues.
    Auto-reconnects on connection loss; exits cleanly on cancellation.
    """
    global _local_cache
    RECONNECT_DELAY_S = 2.0
    while True:
        try:
            async with _redis_client.pubsub() as pubsub:
                await pubsub.subscribe(REDIS_CHANNEL)
                async for msg in pubsub.listen():
                    if msg["type"] != "message":
                        continue
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    # Keep local cache fresh for WS initial-snapshot.
                    _local_cache = data
                    await _fan_out_local(data)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(RECONNECT_DELAY_S)


async def info_exchange_startup() -> None:
    """Connect to Redis and start the per-worker subscriber task.
    Falls back silently to single-worker in-process mode if Redis is unavailable.
    """
    global _redis_client, _redis_sub_task
    try:
        client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
        await client.ping()
        _redis_client = client
        _redis_sub_task = asyncio.create_task(_redis_fan_out_task())
    except Exception:
        _redis_client = None  # Redis not reachable — single-worker fallback.


async def info_exchange_shutdown() -> None:
    """Cancel the subscriber task and close the Redis connection cleanly."""
    global _redis_sub_task, _redis_client
    if _redis_sub_task:
        _redis_sub_task.cancel()
        try:
            await _redis_sub_task
        except asyncio.CancelledError:
            pass
        _redis_sub_task = None
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None