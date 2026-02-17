import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psutil

from fastapi import Depends, FastAPI, HTTPException, Request, Response
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


async def _require_active_user_or_api_key(
    request: Request,
    session=Depends(get_async_session),
    browser_user: User | None = Depends(current_optional_user),
) -> User:
    """Authorize either a logged-in browser user (cookie JWT) or an API key client."""

    if browser_user is not None and getattr(browser_user, "is_active", False):
        return browser_user

    api_key = request.headers.get("x-api-key")
    api_user = await _authenticate_slave_api_key(api_key, session)
    if api_user is not None and getattr(api_user, "is_active", False):
        return api_user

    raise HTTPException(status_code=401, detail="Unauthorized")

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
            #drop if client is slow
            if q.full():
                continue
            q.put_nowait(message)

    return {"ok": True}


# ── /api/me  ──  let the browser discover the current user's role ──
@app.get("/api/me")
async def me(user: User = Depends(current_active_user)):
    return {
        "email": user.email,
        "is_superuser": user.is_superuser,
    }


# ── /api/system-stats  ──  admin-only live machine metrics ──
def _read_cpu_temp() -> float | None:
    """Read CPU temperature.

    Linux : /sys/class/thermal/thermal_zone*/temp  then psutil sensors.
    """
    # --- Linux: scan all thermal zones, pick the hottest ---
    tz_dir = Path("/sys/class/thermal")
    if tz_dir.is_dir():
        best = None
        for tz in sorted(tz_dir.glob("thermal_zone*/temp")):
            try:
                val = int(tz.read_text().strip()) / 1000.0
                if best is None or val > best:
                    best = val
            except (ValueError, OSError):
                continue
        if best is not None:
            return best

    return None


def _read_gpu_nvidia() -> dict:
    """Query nvidia-smi for GPU utilisation, temperature, and VRAM."""
    result: dict = {
        "gpu_percent": None, "gpu_temp_c": None, "gpu_name": None,
        "vram_used_mb": None, "vram_total_mb": None, "vram_percent": None,
    }
    if not shutil.which("nvidia-smi"):
        return result
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,temperature.gpu,name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=4,
            text=True,
        ).strip()
        parts = [p.strip() for p in out.split(",")]
        if len(parts) >= 5:
            result["gpu_percent"] = float(parts[0])
            result["gpu_temp_c"] = float(parts[1])
            result["gpu_name"] = parts[2]
            used_mb = float(parts[3])
            total_mb = float(parts[4])
            result["vram_used_mb"] = used_mb
            result["vram_total_mb"] = total_mb
            result["vram_percent"] = round(used_mb / total_mb * 100, 1) if total_mb else None
    except Exception:
        pass
    return result


@app.get("/api/system-stats")
async def system_stats(_: User = Depends(current_superuser)):
    """Return current machine health metrics (admin only)."""
    loop = asyncio.get_running_loop()
    gpu = await loop.run_in_executor(None, _read_gpu_nvidia)
    cpu_temp = await loop.run_in_executor(None, _read_cpu_temp)

    mem = psutil.virtual_memory()

    # Scan all mounted disks
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(usage.total / (1024 ** 3), 2),
                "used_gb": round(usage.used / (1024 ** 3), 2),
                "free_gb": round(usage.free / (1024 ** 3), 2),
                "percent": usage.percent,
            })
        except (PermissionError, OSError):
            continue

    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_temp_c": cpu_temp,
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_percent": mem.percent,
        "gpu_percent": gpu["gpu_percent"],
        "gpu_temp_c": gpu["gpu_temp_c"],
        "gpu_name": gpu["gpu_name"],
        "vram_used_mb": gpu["vram_used_mb"],
        "vram_total_mb": gpu["vram_total_mb"],
        "vram_percent": gpu["vram_percent"],
        "disks": disks,
    }


@app.get("/api/latest")
async def latest(_: User = Depends(_require_active_user_or_api_key)):
    if _latest_payload is None:
        return {"received_at": None, "payload": None}
    return {"received_at": _latest_received_at, "payload": _latest_payload}


@app.get("/api/stream")
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

