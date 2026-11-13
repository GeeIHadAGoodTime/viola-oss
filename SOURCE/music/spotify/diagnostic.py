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
    except Exception as exc:  # a meter fault must not end the check
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
    player = report.get("player")
    if isinstance(player, dict) and player.get("known"):
        # Sound from Spotify is not enough: build 10 passed here on the laptop
        # while Viola's own player said nothing was playing, so it never ducked
        # and the stage stayed blank.
        if not player.get("is_playing") or not player.get("has_track"):
            return "player_not_tracking"
        if not player.get("provider_is_spotify"):
            return "player_shows_another_source"
    return "none"


def player_view() -> dict[str, Any]:
    """What Viola's own player says right now (what ducking and the stage read)."""
    try:
        from playback.engine_manager import _get_player_for_emit

        player = _get_player_for_emit()
        if player is None:
            return {"known": False, "detail": "no player"}
        state = player.state()
        now = getattr(state, "now_playing", None)
        phase = getattr(getattr(player, "_playback_sm", None), "phase", None)
        return {
            "known": True,
            "is_playing": bool(getattr(state, "is_playing", False)),
            "phase": getattr(phase, "name", None),
            "has_track": now is not None,
            "provider_is_spotify": str(getattr(now, "provider", "") or "").lower() in ("spotify", "spotify_cdp"),
            "has_title": bool(getattr(now, "title", None)),
            "has_artwork": bool(getattr(now, "artwork_url", None)),
            "position_s": int(getattr(state, "position", 0) or 0),
            "duration_s": int(getattr(state, "duration", 0) or 0),
        }
    except Exception as exc:  # a diagnostic must never raise
        return {"known": False, "detail": type(exc).__name__}


def log_diagnostic(controller: Any, *, reason: str) -> dict[str, Any]:
    """Run the check, write it to the log and return it."""
    report = controller.diagnose()
    report["audio"] = browser_audio_peak(report.get("browser_pid")) if report.get("expected_to_play") else {}
    report["player"] = player_view() if report.get("expected_to_play") else {}
    report["reason"] = reason
    report["first_failure"] = _first_failure(report)
    for key in sorted(report):
        logger.info("SPOTIFY_DIAG %s=%s", key, report[key])
    if report.get("expected_to_play") and report["first_failure"] == "none":
        # The player's side is in the SPOTIFY_DIAG player= line; this line only
        # appears when the player follows the song too (player_not_tracking).
        logger.info(
            "SPOTIFY_PROOF provider=spotify track_id=%s position_moved_s=%s browser_audio_peak=%s",
            report.get("track_id"),
            report.get("position_moved_s"),
            (report.get("audio") or {}).get("peak"),
        )
    logger.info("SPOTIFY_DIAG verdict reason=%s first_failure=%s", reason, report["first_failure"])
    return report


__all__ = ["browser_audio_peak", "log_diagnostic", "player_view"]
