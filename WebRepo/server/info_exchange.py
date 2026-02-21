import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from server.models import User
from server.auth import _require_active_user_or_api_key, get_async_session, _authenticate_slave_api_key

info_exchange_router = APIRouter()

_latest_payload: dict | None = None
_latest_received_at: float | None = None
_subscribers: set[asyncio.Queue[str]] = set()
_subscribers_lock = asyncio.Lock()

#Responds to follower request (*Outdated)
@info_exchange_router.get("/api/latest")
async def latest(_: User = Depends(_require_active_user_or_api_key)):
    if _latest_payload is None:
        return {"received_at": None, "payload": None}
    return {"received_at": _latest_received_at, "payload": _latest_payload}

#Recieves leader action
@info_exchange_router.post("/api/ingest")
async def ingest(payload: dict, request: Request, session=Depends(get_async_session)):
    """API for the slave node to publish the latest telemetry snapshot.

    Auth: `X-API-Key: <SLAVE_API_KEY>`.
    """
    api_key = request.headers.get("x-api-key")
    user = await _authenticate_slave_api_key(api_key, session)
    if not user:
        return PlainTextResponse("Unauthorized", status_code=401)

    global _latest_payload, _latest_received_at
    _latest_payload = payload
    _latest_received_at = time.time()

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
            #drop if client is slow
            if q.full():
                continue
            q.put_nowait(message)

    return {"ok": True}

@info_exchange_router.get("/api/stream")
async def stream(request: Request, _: User = Depends(_require_active_user_or_api_key)):
    async def event_generator():
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10)
        async with _subscribers_lock:
            _subscribers.add(queue)

        last_keepalive = 0.0

        try:
            # Send current snapshot immediately if available.
            if _latest_payload is not None:
                initial = json.dumps(
                    {"received_at": _latest_received_at, "payload": _latest_payload},
                    separators=(",", ":"),
                )
                yield f"data: {initial}\n\n"

            # Stream updates + periodic keepalive.
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Small timeout so we notice disconnects quickly.
                    msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    # Check disconnect again, then emit keepalive at most every 15s.
                    if await request.is_disconnected():
                        break
                    now = time.time()
                    if now - last_keepalive >= 15.0:
                        last_keepalive = now
                        yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            async with _subscribers_lock:
                _subscribers.discard(queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # For nginx: disable proxy buffering for SSE
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)