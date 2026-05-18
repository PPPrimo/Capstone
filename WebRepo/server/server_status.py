import asyncio
import collections
import json
import shutil
import subprocess
import time as _time
import psutil

from pathlib import Path
from fastapi import APIRouter, Depends

from server.auth import current_superuser
from server.models import User

server_status_router = APIRouter()

# ── Background stats collection ──
_SENSITIVE_DIR = Path(__file__).resolve().parent / "sensitive"
_HISTORY_FILE = _SENSITIVE_DIR / "stats_history.json"
_MAX_ENTRIES = 80_000
_MAX_FILE_BYTES = 5 * 1024 ** 3  # 5 GB rolling cap on the JSON file
_COLLECT_INTERVAL_S = 2.0
_PERSIST_INTERVAL_S = 60.0

_stats_history: collections.deque = collections.deque(maxlen=_MAX_ENTRIES)
_history_lock = asyncio.Lock()

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


@server_status_router.get("/api/system-stats")
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


def _collect_stats_sync() -> dict:
    """Collect system stats synchronously — intended to run in a thread executor."""
    gpu = _read_gpu_nvidia()
    cpu_temp = _read_cpu_temp()
    mem = psutil.virtual_memory()
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


def _raw_to_entry(raw: dict, t_ms: float) -> dict:
    """Convert a raw stats dict to the compact entry format used by the browser's localStorage."""
    entry: dict = {
        "t": t_ms,
        "cpu": raw.get("cpu_percent"),
        "ram": raw.get("ram_percent"),
        "ramU": raw.get("ram_used_gb"),
        "ramT": raw.get("ram_total_gb"),
        "gpu": raw.get("gpu_percent"),
        "vram": raw.get("vram_percent"),
        "cpuT": raw.get("cpu_temp_c"),
        "gpuT": raw.get("gpu_temp_c"),
    }
    for dk in (raw.get("disks") or []):
        entry["d_" + dk["mountpoint"]] = dk["percent"]
    return entry


async def _load_persisted_history() -> None:
    """Load previously persisted history from disk into the in-memory deque on startup."""
    if not _HISTORY_FILE.exists():
        return
    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, lambda: _HISTORY_FILE.read_text("utf-8"))
        # Each line is one JSON entry (newest first); reverse so deque is ascending.
        # Also handle legacy format where a line may be a JSON array.
        lines = [l for l in text.splitlines() if l.strip()]
        entries = []
        for l in lines:
            parsed = json.loads(l)
            if isinstance(parsed, list):
                entries.extend(parsed)   # flatten legacy array lines
            elif isinstance(parsed, dict):
                entries.append(parsed)
        entries.sort(key=lambda e: e.get("t", 0))
        async with _history_lock:
            for entry in entries[-_MAX_ENTRIES:]:
                _stats_history.append(entry)
    except Exception:
        pass  # Corrupt or missing file — start fresh


def _trim_to_size(entries: list) -> list:
    """Drop oldest entries until the serialized payload fits within _MAX_FILE_BYTES."""
    while entries:
        lines = [json.dumps(e, separators=(",", ":")) for e in reversed(entries)]
        size = sum(len(l.encode("utf-8")) + 1 for l in lines)  # +1 for newline
        if size <= _MAX_FILE_BYTES:
            break
        entries = entries[1:]  # drop oldest
    return entries


async def persist_stats_history() -> None:
    """Flush the in-memory history deque to disk, enforcing the 5 GB rolling cap."""
    async with _history_lock:
        snapshot = list(_stats_history)
    if not snapshot:
        return
    try:
        loop = asyncio.get_running_loop()
        trimmed = await loop.run_in_executor(None, _trim_to_size, snapshot)
        # Sync the deque if entries were trimmed to keep memory consistent
        if len(trimmed) < len(snapshot):
            async with _history_lock:
                _stats_history.clear()
                for e in trimmed:
                    _stats_history.append(e)
        # One JSON entry per line, newest entry last (ascending) so the browser
        # always loads the most recent data first after a page reload.
        lines = [json.dumps(e, separators=(",", ":")) for e in trimmed]
        payload = "\n".join(lines) + "\n"
        await loop.run_in_executor(
            None, lambda: _HISTORY_FILE.write_text(payload, "utf-8")
        )
    except Exception:
        pass


async def stats_collector_task() -> None:
    """Background task: sample system stats every 2 s and persist to disk every 60 s.
    Runs for as long as the server is up — independent of any browser connection.
    """
    await _load_persisted_history()
    loop = asyncio.get_running_loop()
    # Subtract the interval so the first persist fires on the very first collection cycle
    last_persist = _time.monotonic() - _PERSIST_INTERVAL_S
    while True:
        try:
            raw = await loop.run_in_executor(None, _collect_stats_sync)
            entry = _raw_to_entry(raw, _time.time() * 1000)
            async with _history_lock:
                _stats_history.append(entry)
        except Exception:
            pass
        now = _time.monotonic()
        if now - last_persist >= _PERSIST_INTERVAL_S:
            await persist_stats_history()
            last_persist = now
        await asyncio.sleep(_COLLECT_INTERVAL_S)


@server_status_router.get("/api/system-history")
async def system_history(_: User = Depends(current_superuser)):
    """Return the full server-side stats history (admin only)."""
    async with _history_lock:
        return list(_stats_history)


@server_status_router.delete("/api/system-history")
async def clear_system_history(_: User = Depends(current_superuser)):
    """Clear the in-memory stats history and delete the persisted JSON file (admin only)."""
    async with _history_lock:
        _stats_history.clear()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _HISTORY_FILE.unlink(missing_ok=True))
    except Exception:
        pass
    # Schedule an immediate collect + persist in the background so the file
    # and in-memory deque are repopulated without waiting up to 60 s.
    asyncio.create_task(_collect_and_persist_once())
    return {"ok": True, "message": "Stats history cleared."}


async def _collect_and_persist_once() -> None:
    """Collect one stats snapshot and persist it to disk immediately."""
    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, _collect_stats_sync)
        entry = _raw_to_entry(raw, _time.time() * 1000)
        async with _history_lock:
            _stats_history.append(entry)
        await persist_stats_history()
    except Exception:
        pass