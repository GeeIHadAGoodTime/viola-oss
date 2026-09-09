"""System info handler — gathers system metrics."""

from __future__ import annotations

import platform
import time

from core.logging_config import get_logger
from plugins.api import PluginResponse

logger = get_logger(__name__)

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


def get_system_status() -> PluginResponse:
    """Get overall system status."""
    if not _PSUTIL_AVAILABLE:
        return PluginResponse(
            speech="System monitoring requires the psutil library, which isn't installed.",
            error="psutil not available",
        )

    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        speech = (
            "System is running on %s. "
            "CPU is at %d percent. "
            "Memory is %d percent used, with %.1f gigabytes available. "
            "Disk is %d percent full."
            % (
                platform.system(),
                cpu_pct,
                mem.percent,
                mem.available / (1024**3),
                disk.percent,
            )
        )

        return PluginResponse(
            speech=speech,
            display={
                "platform": platform.system(),
                "cpu_percent": cpu_pct,
                "memory_percent": mem.percent,
                "memory_available_gb": round(mem.available / (1024**3), 1),
                "disk_percent": disk.percent,
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError, psutil.Error) as exc:
        logger.warning("System status error: %s", exc)
        return PluginResponse(
            speech="I couldn't get the system status right now.",
            error=str(exc),
        )


def get_cpu_info() -> PluginResponse:
    """Get CPU usage info."""
    if not _PSUTIL_AVAILABLE:
        return PluginResponse(
            speech="CPU monitoring requires psutil.",
            error="psutil not available",
        )

    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq()

        freq_str = ""
        if cpu_freq:
            freq_str = " running at %.0f megahertz" % cpu_freq.current

        speech = "CPU usage is %d percent across %d cores%s." % (
            cpu_pct,
            cpu_count or 0,
            freq_str,
        )

        return PluginResponse(
            speech=speech,
            display={
                "cpu_percent": cpu_pct,
                "cpu_count": cpu_count,
                "cpu_freq_mhz": cpu_freq.current if cpu_freq else None,
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError, psutil.Error) as exc:
        logger.warning("CPU info error: %s", exc)
        return PluginResponse(speech="I couldn't get CPU info.", error=str(exc))


def get_disk_info() -> PluginResponse:
    """Get disk usage info."""
    if not _PSUTIL_AVAILABLE:
        return PluginResponse(
            speech="Disk monitoring requires psutil.",
            error="psutil not available",
        )

    try:
        disk = psutil.disk_usage("/")
        total_gb = disk.total / (1024**3)
        used_gb = disk.used / (1024**3)
        free_gb = disk.free / (1024**3)

        speech = (
            "You're using %.1f of %.1f gigabytes of disk space. "
            "That's %d percent, with %.1f gigabytes free." % (used_gb, total_gb, disk.percent, free_gb)
        )

        return PluginResponse(
            speech=speech,
            display={
                "total_gb": round(total_gb, 1),
                "used_gb": round(used_gb, 1),
                "free_gb": round(free_gb, 1),
                "percent": disk.percent,
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError, psutil.Error) as exc:
        logger.warning("Disk info error: %s", exc)
        return PluginResponse(speech="I couldn't get disk info.", error=str(exc))


def get_memory_info() -> PluginResponse:
    """Get memory usage info."""
    if not _PSUTIL_AVAILABLE:
        return PluginResponse(
            speech="Memory monitoring requires psutil.",
            error="psutil not available",
        )

    try:
        mem = psutil.virtual_memory()
        used_gb = mem.used / (1024**3)
        total_gb = mem.total / (1024**3)
        avail_gb = mem.available / (1024**3)

        speech = (
            "You're using %.1f of %.1f gigabytes of memory. "
            "That's %d percent, with %.1f gigabytes available." % (used_gb, total_gb, mem.percent, avail_gb)
        )

        return PluginResponse(
            speech=speech,
            display={
                "used_gb": round(used_gb, 1),
                "total_gb": round(total_gb, 1),
                "available_gb": round(avail_gb, 1),
                "percent": mem.percent,
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError, psutil.Error) as exc:
        logger.warning("Memory info error: %s", exc)
        return PluginResponse(speech="I couldn't get memory info.", error=str(exc))


def get_uptime_info() -> PluginResponse:
    """Get system uptime."""
    if not _PSUTIL_AVAILABLE:
        return PluginResponse(
            speech="Uptime monitoring requires psutil.",
            error="psutil not available",
        )

    try:
        boot_time = psutil.boot_time()
        uptime_seconds = int(time.time() - boot_time)

        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60

        parts = []
        if days > 0:
            parts.append("%d day%s" % (days, "s" if days != 1 else ""))
        if hours > 0:
            parts.append("%d hour%s" % (hours, "s" if hours != 1 else ""))
        if minutes > 0:
            parts.append("%d minute%s" % (minutes, "s" if minutes != 1 else ""))

        duration = ", ".join(parts) if parts else "less than a minute"
        speech = "The system has been running for %s." % duration

        return PluginResponse(
            speech=speech,
            display={
                "uptime_seconds": uptime_seconds,
                "days": days,
                "hours": hours,
                "minutes": minutes,
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError, psutil.Error) as exc:
        logger.warning("Uptime info error: %s", exc)
        return PluginResponse(speech="I couldn't get the uptime.", error=str(exc))
