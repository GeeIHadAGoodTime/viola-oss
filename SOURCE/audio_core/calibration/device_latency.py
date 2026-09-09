"""
Tier A Device Output Latency Detection.

Queries the OS audio subsystem for output device latency information.
Detects Bluetooth devices and applies codec-based latency estimates.

No test tones, no user interruption, runs at spoke startup.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Bluetooth device name patterns (case-insensitive matching)
BLUETOOTH_PATTERNS = [
    "bluetooth",
    "bt ",
    "airpods",
    "wh-1000",
    "wf-1000",
    "jbl ",
    "sony wh",
    "sony wf",
    "bose ",
    "galaxy buds",
    "beats ",
    "jabra",
    "sennheiser momentum",
    "pixel buds",
    "nothing ear",
    "edifier",
]

# Conservative Bluetooth codec latency estimates (ms)
# Can't detect actual codec from device name alone
BLUETOOTH_SBC_LATENCY_MS = 150.0  # Default/SBC (most common)
BLUETOOTH_APTX_LATENCY_MS = 40.0
BLUETOOTH_APTX_HD_LATENCY_MS = 80.0
BLUETOOTH_AAC_LATENCY_MS = 40.0
# Use SBC as default since we can't detect codec
BLUETOOTH_DEFAULT_LATENCY_MS = BLUETOOTH_SBC_LATENCY_MS


def detect_output_latency() -> dict[str, Any]:
    """
    Detect the output latency of the default audio output device.

    Returns:
        dict with keys:
            - latency_ms: float - estimated output latency in milliseconds
            - method: str - detection method used ("sounddevice_query", "fallback")
            - confidence: str - "high" for wired, "medium" for Bluetooth
            - device_name: str - name of the detected device
            - is_bluetooth: bool - whether device appears to be Bluetooth
    """
    try:
        import sounddevice as sd

        device_info = sd.query_devices(kind="output")
        if device_info is None:
            return _fallback_result("No output device found")

        device_name = device_info.get("name", "Unknown")
        # sounddevice reports latency in seconds
        default_low_latency = device_info.get("default_low_output_latency", 0)

        # Use low latency estimate (typical for real-time audio)
        os_latency_ms = default_low_latency * 1000.0

        # Check if Bluetooth
        is_bt = _is_bluetooth_device(device_name)

        if is_bt:
            # Bluetooth: add codec latency estimate to OS-reported latency
            total_latency = os_latency_ms + BLUETOOTH_DEFAULT_LATENCY_MS
            logger.info(
                "Bluetooth device detected: %s, os_latency=%.1fms, " "codec_estimate=%.1fms, total=%.1fms",
                device_name,
                os_latency_ms,
                BLUETOOTH_DEFAULT_LATENCY_MS,
                total_latency,
            )
            return {
                "latency_ms": total_latency,
                "method": "sounddevice_query",
                "confidence": "medium",
                "device_name": device_name,
                "is_bluetooth": True,
                "os_latency_ms": os_latency_ms,
                "codec_estimate_ms": BLUETOOTH_DEFAULT_LATENCY_MS,
            }

        # Wired: OS-reported latency is reliable
        logger.info(
            "Wired device detected: %s, latency=%.1fms",
            device_name,
            os_latency_ms,
        )
        return {
            "latency_ms": os_latency_ms,
            "method": "sounddevice_query",
            "confidence": "high",
            "device_name": device_name,
            "is_bluetooth": False,
        }

    except ImportError:
        logger.warning("sounddevice not available, using fallback latency detection")
        return _fallback_result("sounddevice not installed")
    except Exception as e:
        logger.warning("Device latency detection failed: %s", e)
        return _fallback_result(str(e))


def _is_bluetooth_device(device_name: str) -> bool:
    """Check if a device name suggests Bluetooth."""
    name_lower = device_name.lower()
    return any(pattern in name_lower for pattern in BLUETOOTH_PATTERNS)


def _fallback_result(reason: str) -> dict[str, Any]:
    """Return a default fallback result when detection fails."""
    # Default to 10ms (typical wired USB/analog latency)
    return {
        "latency_ms": 10.0,
        "method": "fallback",
        "confidence": "low",
        "device_name": "unknown",
        "is_bluetooth": False,
        "fallback_reason": reason,
    }
