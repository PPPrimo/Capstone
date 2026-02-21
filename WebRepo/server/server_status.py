import asyncio
import shutil
import subprocess
import psutil

from pathlib import Path
from fastapi import APIRouter, Depends

from server.auth import current_superuser
from server.models import User

server_status_router = APIRouter()

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