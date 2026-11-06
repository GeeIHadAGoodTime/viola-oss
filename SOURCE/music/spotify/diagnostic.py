"""One check of every link from a Spotify sign-in to sound, written to the log.

Viola can't be tested with a real Spotify account in development, so the last
step is always proved on a real install. This runs after a sign-in, after every
failed play and a few seconds into every play, and writes ``SPOTIFY_DIAG`` lines:
one per link and a verdict naming the first link that failed. One run on the
laptop therefore pinpoints any remaining fault, and "it played" is provable: the
provider, the Spotify track id, the moving position and the browser's own audio
session are all in the log.

Nothing secret is logged: cookie presence only (never values), and no account
or device names.
"""

from __future__ import annotations

import time
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_AUDIO_SAMPLES = 8
_AUDIO_SAMPLE_GAP_S = 0.25


def browser_audio_peak(root_pid: int | None) -> dict[str, Any]:
    """Read the loudest level of the playback browser's own Windows audio session."""
    if not root_pid:
        return {"session_found": False, "peak": 0.0, "detail": "no browser"}
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        from music.spotify.cdp_controller import _chrome_process_tree
    except (ImportError, OSError) as exc:
        return {"session_found": False, "peak": 0.0, "detail": "meter unavailable: %s" % type(exc).__name__}

    comtypes.CoInitialize()
    try:
        pids = _chrome_process_tree(root_pid)
        peak = 0.0
        found = False
        for _ in range(_AUDIO_SAMPLES):
            for session in AudioUtilities.GetAllSessions():
                if session.ProcessId not in pids:
                    continue
                found = True
                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                peak = max(peak, float(meter.GetPeakValue()))
            time.sleep(_AUDIO_SAMPLE_GAP_S)
        return {"session_found": found, "peak": round(peak, 4)}
    except Exception as exc:  # noqa: BLE001 - a meter fault must not end the check
        return {"session_found": False, "peak": 0.0, "detail": type(exc).__name__}
    finally:
        comtypes.CoUninitialize()


def _first_failure(report: dict[str, Any]) -> str:
    """The first link in the chain that isn't working."""
    if report.get("page_auth_state") == "session_rejected":
        return "spotify_refused_sign_in"
    if not report.get("in_app_sign_in") and not report.get("player_sign_in_cookie"):
        return "no_sign_in"
    if not report.get("browser_running"):
        return "browser_not_running"
    if report.get("window_visible"):
        return "window_visible"
    if report.get("microsoft_account_in_profile"):
        return "microsoft_account_in_profile"
    if not report.get("player_sign_in_cookie"):
        return "cookie_not_in_player"
    if report.get("page_auth_state") != "authenticated":
        return "page_not_signed_in"
    if not report.get("widevine_ok"):
        return "drm"
    if not report.get("expected_to_play"):
        return "none"
    if report.get("other_device_active"):
        return "another_device_is_playing"
    if not report.get("position_moving"):
        return "position_not_moving"
    audio = report.get("audio") or {}
    if not audio.get("session_found") or not audio.get("peak"):
        return "no_browser_audio"
    return "none"


def log_diagnostic(controller: Any, *, reason: str) -> dict[str, Any]:
    """Run the check, write it to the log and return it."""
    report = controller.diagnose()
    report["audio"] = browser_audio_peak(report.get("browser_pid")) if report.get("expected_to_play") else {}
    report["reason"] = reason
    report["first_failure"] = _first_failure(report)
    for key in sorted(report):
        logger.info("SPOTIFY_DIAG %s=%s", key, report[key])
    if report.get("expected_to_play") and report["first_failure"] == "none":
        logger.info(
            "SPOTIFY_PROOF provider=spotify track_id=%s position_moved_s=%s browser_audio_peak=%s",
            report.get("track_id"),
            report.get("position_moved_s"),
            (report.get("audio") or {}).get("peak"),
        )
    logger.info("SPOTIFY_DIAG verdict reason=%s first_failure=%s", reason, report["first_failure"])
    return report


__all__ = ["browser_audio_peak", "log_diagnostic"]
