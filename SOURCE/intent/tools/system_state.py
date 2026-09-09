"""System state tool for the agent.

Provides read-only system metrics (CPU, RAM, disk, top processes)
using psutil with graceful fallback if not installed.
"""

from __future__ import annotations

import platform

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)


async def system_info() -> ToolResult:
    """Get current system information (CPU, RAM, disk usage, top processes)."""
    info: dict[str, object] = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }

    try:
        import psutil

        # CPU
        info["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        info["cpu_count"] = psutil.cpu_count()

        # Memory
        mem = psutil.virtual_memory()
        info["memory"] = {
            "total_gb": round(mem.total / (1024**3), 1),
            "used_gb": round(mem.used / (1024**3), 1),
            "percent": mem.percent,
        }

        # Disk
        partitions = []
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                partitions.append(
                    {
                        "mountpoint": part.mountpoint,
                        "total_gb": round(usage.total / (1024**3), 1),
                        "used_gb": round(usage.used / (1024**3), 1),
                        "percent": usage.percent,
                    }
                )
            except (PermissionError, OSError):
                continue
        info["disk"] = partitions

        # Top processes by memory
        procs = []
        for proc in sorted(
            psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]),
            key=lambda p: p.info.get("memory_percent") or 0.0,
            reverse=True,
        )[:10]:
            procs.append(
                {
                    "pid": proc.info["pid"],
                    "name": proc.info["name"],
                    "memory_percent": round(proc.info.get("memory_percent") or 0.0, 1),
                    "cpu_percent": round(proc.info.get("cpu_percent") or 0.0, 1),
                }
            )
        info["top_processes"] = procs

    except ImportError:
        info["note"] = "psutil not installed - limited system info available"
        logger.debug("psutil not available for system_info tool")

    return ToolResult(ok=True, data=info)
