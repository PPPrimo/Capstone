import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from sqlalchemy import select

from server.auth import auth_backend, cookie_transport, current_active_user, current_optional_user, current_superuser, fastapi_users
from server.db import create_db_and_tables, get_async_session
from server.models import User

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = WORKSPACE_DIR / "public"


app = FastAPI()

# --- Telemetry: slave -> server -> browser (SSE) ---
APP_ENV = os.getenv("APP_ENV", "development").lower()

_latest_payload: dict | None = None
_latest_received_at: float | None = None
_subscribers: set[asyncio.Queue[str]] = set()
_subscribers_lock = asyncio.Lock()


async def _authenticate_slave_api_key(api_key: str | None, session) -> User | None:
    if not api_key:
        return None
    if "." not in api_key:
        return None
    prefix = api_key.split(".", 1)[0]
    if not prefix:
        return None

    user = (await session.execute(select(User).where(User.api_key_prefix == prefix))).scalar_one_or_none()
    if not user or not user.api_key_hash:
        return None

    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    if hmac.compare_digest(digest, user.api_key_hash):
        return user
    return None

@app.on_event("startup")
async def on_startup():
    await create_db_and_tables()

@app.on_event("shutdown")
async def on_shutdown():
    pass

# Framework-managed auth routes:
# - POST /auth/jwt/login
# - POST /auth/jwt/logout
app.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/jwt",
    tags=["auth"],
)


@app.middleware("http")
async def security_headers_and_methods(request: Request, call_next):
    # Block access to internal directories (defense-in-depth)
    path = request.url.path
    if path.startswith(("/server/", "/tools/", "/dev_local/")):
        return PlainTextResponse("Forbidden", status_code=403)

    # Allow only safe methods globally; allow POST for login/logout and slave ingest.
    if request.method not in ("GET", "HEAD"):
        if request.method == "POST" and path in ("/auth/jwt/login", "/auth/jwt/logout"):
            pass
        elif request.method == "POST" and path == "/api/ingest":
            pass
        else:
            return PlainTextResponse("Method Not Allowed", status_code=405)

    response: Response = await call_next(request)

    # Best-effort: discourage caching and reduce accidental downloads
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


@app.post("/api/ingest")
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
            # Best-effort (drop if client is slow)
            if q.full():
                continue
            q.put_nowait(message)

    return {"ok": True}


@app.get("/api/latest")
async def latest(user=Depends(current_active_user)):
    if _latest_payload is None:
        return {"received_at": None, "payload": None}
    return {"received_at": _latest_received_at, "payload": _latest_payload}


@app.get("/api/stream")
async def stream(request: Request, user=Depends(current_active_user)):
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
    
@app.get("/login.html")
async def login_page():
    return FileResponse(PUBLIC_DIR / "login.html")

@app.get("/api/logout")
async def logout():
    # Convenience GET endpoint for the UI; FastAPI-Users official logout is POST /auth/jwt/logout.
    resp = RedirectResponse("/login.html", status_code=302)
    resp.delete_cookie(cookie_transport.cookie_name)
    return resp

# Explicit gates for primary pages to ensure redirect occurs even if static mount is hit
@app.get("/")
async def gate_root(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "index.html")

@app.get("/index.html")
async def gate_index(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "index.html")

@app.get("/feature1.html")
async def gate_f1(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "feature1.html")

@app.get("/feature2.html")
async def gate_f2(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "feature2.html")

@app.get("/feature3.html")
async def gate_f3(user=Depends(current_optional_user)):
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return FileResponse(PUBLIC_DIR / "feature3.html")

# Serve the entire website directory as static content
app.mount(
    "/",
    StaticFiles(directory=str(PUBLIC_DIR), html=True),
    name="static",
)

