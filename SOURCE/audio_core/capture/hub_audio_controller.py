"""
Hub Audio Controller — PID discovery and session mute/unmute via pycaw.

Provides shared utilities for:
- Finding the child process (e.g. YouTube renderer) with an active audio session
- Muting/unmuting that process's audio session independently of other apps

Used by ProcTapProvider for process capture and by providers such as Spotify
CDP to register external browser audio PIDs.

Instead of SetMute (which silences ProcTap loopback capture), we reduce
the session volume to QUIET_VOLUME (1%).  ProcTap still receives audio
at reduced amplitude, and the capture pipeline amplifies it back.  The
user hears only 1% volume from the direct path — effectively inaudible.

COM threading note:
    pycaw imports comtypes, which calls CoInitializeEx() at module load time.
    If the calling thread already has COM in a different apartment mode this
    raises OSError.  _ensure_com_mta() pre-initializes COM in MTA before the
    pycaw import so comtypes sees an already-initialized apartment and skips
    its own init.
"""

from __future__ import annotations

import os

from core.logging_config import get_logger

logger = get_logger(__name__)

_SESSION_STATE_ACTIVE = 1

# External audio PIDs registered by non-child providers (e.g. Spotify CDP
# Chrome instance).  ProcTap normally discovers child PIDs via psutil, but
# providers that launch separate processes can register their PID here so
# find_audio_child_pid() includes it in the scan.
_external_audio_pids: set[int] = set()

# Excluded PIDs — processes whose audio sessions must NOT be captured by
# ProcTap. Used to prevent feedback loops when a local audio subprocess would
# otherwise be recaptured.
_excluded_pids: set[int] = set()


def register_external_audio_pid(pid: int) -> None:
    """Register an external process PID as a valid audio capture target.

    Call this when a provider launches a separate process (e.g. Spotify CDP
    Chrome) whose audio should be captured by ProcTap for multi-room streaming.

    Args:
        pid: Process ID to add to the capture candidate set.
    """
    _external_audio_pids.add(pid)
    logger.info("Registered external audio PID %d (total=%d)", pid, len(_external_audio_pids))


def unregister_external_audio_pid(pid: int) -> None:
    """Remove an external PID from the capture candidate set.

    Call on shutdown or when the external process exits.

    Args:
        pid: Process ID to remove.
    """
    _external_audio_pids.discard(pid)
    logger.info("Unregistered external audio PID %d (total=%d)", pid, len(_external_audio_pids))


def exclude_pid(pid: int) -> None:
    """Exclude a PID from audio capture.

    ProcTap will not capture from this PID even if it has an active audio
    session.  Used to prevent feedback loops.

    Args:
        pid: Process ID to exclude from the capture candidate set.
    """
    _excluded_pids.add(pid)
    logger.info("Excluded PID %d from audio capture (total=%d)", pid, len(_excluded_pids))


def include_pid(pid: int) -> None:
    """Re-include a previously excluded PID for audio capture.

    Args:
        pid: Process ID to re-include.
    """
    _excluded_pids.discard(pid)
    logger.info("Re-included PID %d for audio capture (total=%d)", pid, len(_excluded_pids))


# Volume level used to "mute" the process.  Must be > 0 so ProcTap
# still captures audio.  The capture pipeline amplifies by 1/QUIET_VOLUME.
#
# History:
#   0.01 (1%) — too low with int16 capture (Bug 38, quantization noise).
#               Safe with float32 capture (upgraded 2026-02-27).
#   0.10 (10%) — was used as int16-safe compromise but audible from speakers.
#
# With float32 capture (~144 dB dynamic range), 1% provides ~40 dB of
# usable signal above the noise floor.  Hub user hears YouTube at 1%
# (effectively silent) while spokes get full-amplitude PCM via 100x gain.
QUIET_VOLUME = 0.01


def _ensure_com_mta() -> None:
    """Pre-initialize COM in MTA mode on the current thread.

    Must be called BEFORE importing pycaw/comtypes so that the comtypes
    module-level CoInitializeEx() finds an already-initialized apartment
    and does not conflict.  Safe to call multiple times.
    """
    try:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # Already initialized — acceptable
    except ImportError:
        try:
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
        except Exception:
            logger.debug("COM MTA initialization failed via ctypes fallback", exc_info=True)


def _get_candidate_pids() -> set[int]:
    """Build the set of PIDs eligible for audio capture.

    Includes:
    - Viola's own PID (for --disable-features=AudioServiceOutOfProcess)
    - All child processes (recursive) via psutil
    - Any externally registered PIDs (e.g. Spotify CDP Chrome)
    """
    try:
        import psutil
    except ImportError:
        return set()

    try:
        my_pid = os.getpid()
        parent = psutil.Process(my_pid)
        child_pids = {c.pid for c in parent.children(recursive=True)}
        return (child_pids | {my_pid} | _external_audio_pids) - _excluded_pids
    except Exception:
        logger.exception("Error building candidate PID set")
        return set()


def _session_is_active(session: object) -> bool:
    """Return True when a pycaw session is actively producing or readying audio.

    Mere session presence is too broad for ProcTap discovery: Chromium, Qt, and
    Windows can leave inactive sessions around long after audio has stopped.
    Prefer the documented session ``State`` when available, and fall back to the
    peak meter for environments where the state enum is not exposed.
    """
    state = getattr(session, "State", None)
    if state == _SESSION_STATE_ACTIVE:
        return True

    try:
        meter = getattr(session, "AudioMeterInformation", None)
        if meter is not None and meter.GetPeakValue() > 0.0:
            return True
    except Exception:
        logger.debug("Audio meter probe failed during PID discovery")

    # If pycaw does not expose a session state, preserve legacy behavior rather
    # than disabling capture entirely on that platform/build.
    return state is None


def find_audio_child_pid() -> int | None:
    """Find a process with an active audio session from the candidate set.

    Scans child processes (recursive), the main Viola PID, and any
    externally registered PIDs against pycaw audio sessions.
    This is the shared version used by ProcTap discovery and source-volume control.

    Priority order (Bug #29 Fix 5):
    1. Externally registered PIDs (e.g. Spotify CDP Chrome)
    2. Viola's own PID (local file playback via sounddevice)
    3. Any other child process (QtWebEngine renderer, etc.)

    Returns:
        PID with an active audio session, or None.
    """
    _ensure_com_mta()

    try:
        from pycaw.pycaw import AudioUtilities
    except (ImportError, OSError):
        logger.debug("pycaw not available for PID discovery")
        return None

    candidate_pids = _get_candidate_pids()
    if not candidate_pids:
        return None

    my_pid = os.getpid()

    try:
        # Collect all matching PIDs first, then pick by priority.
        active_pids: set[int] = set()
        for session in AudioUtilities.GetAllSessions():
            if session.ProcessId in candidate_pids and _session_is_active(session):
                active_pids.add(session.ProcessId)

        if not active_pids:
            return None

        # Priority 1: externally registered PIDs (provider-specific)
        ext_matches = active_pids & _external_audio_pids
        if ext_matches:
            pid = next(iter(ext_matches))
            logger.debug("Found audio PID %d (external)", pid)
            return pid

        # Priority 2: Viola's own PID (local file playback)
        if my_pid in active_pids:
            logger.debug("Found audio PID %d (self)", my_pid)
            return my_pid

        # Priority 3: any other child process
        pid = next(iter(active_pids))
        logger.debug("Found audio PID %d (child)", pid)
        return pid

    except Exception:
        logger.exception("Error discovering audio PID")

    return None


def find_all_audio_pids() -> list[int]:
    """Find ALL processes with active audio sessions from the candidate set.

    Unlike find_audio_child_pid() which returns only the first match, this
    returns all matches.  Used by ProcTap to detect when a different audio
    source becomes available (e.g. provider switch from YouTube to Spotify).

    Returns:
        List of PIDs with active audio sessions (may be empty).
    """
    _ensure_com_mta()

    try:
        from pycaw.pycaw import AudioUtilities
    except (ImportError, OSError):
        return []

    candidate_pids = _get_candidate_pids()
    if not candidate_pids:
        return []

    try:
        result = []
        for session in AudioUtilities.GetAllSessions():
            if session.ProcessId in candidate_pids and _session_is_active(session):
                result.append(session.ProcessId)
        return result

    except Exception:
        logger.exception("Error discovering audio PIDs")
        return []


def mute_session(pid: int) -> bool:
    """Suppress the audio session for the given PID.

    Sets the per-process volume to QUIET_VOLUME (1%) instead of using
    SetMute.  ProcTap's per-process loopback captures audio post-mixer,
    so SetMute/SetMasterVolume(0) would deliver silence to ProcTap.
    A tiny but non-zero volume keeps the signal flowing through ProcTap
    while being effectively inaudible from speakers.

    The capture pipeline compensates by amplifying by 1/QUIET_VOLUME.

    Args:
        pid: Process ID whose audio session should be suppressed.

    Returns:
        True if the session was found and volume reduced, False otherwise.
    """
    _ensure_com_mta()

    try:
        from pycaw.pycaw import AudioUtilities

        all_sessions = AudioUtilities.GetAllSessions()
        all_pids = [s.ProcessId for s in all_sessions]
        logger.warning(
            "[HUB-MUTE-DIAG] mute_session(pid=%d): found %d sessions, pids=%s",
            pid,
            len(all_pids),
            all_pids,
        )

        found = False
        for session in all_sessions:
            if session.ProcessId == pid:
                # Ensure not muted (unmute first, in case a previous run left it muted)
                session.SimpleAudioVolume.SetMute(0, None)
                session.SimpleAudioVolume.SetMasterVolume(QUIET_VOLUME, None)
                # Read back to confirm the volume change took effect
                actual_vol = session.SimpleAudioVolume.GetMasterVolume()
                logger.warning(
                    "[HUB-MUTE-DIAG] mute_session: SET pid=%d volume=%.4f " "readback=%.4f (target=%.4f) success=%s",
                    pid,
                    QUIET_VOLUME,
                    actual_vol,
                    QUIET_VOLUME,
                    abs(actual_vol - QUIET_VOLUME) < 0.005,
                )
                logger.info(
                    "Suppressed audio session for PID %d (volume=%.3f)",
                    pid,
                    QUIET_VOLUME,
                )
                found = True
                # Continue loop: mute ALL sessions for this PID.
                # Chromium/QtWebEngine can create multiple WASAPI sessions for
                # the same PID (e.g. a new session while an old one is still
                # active).  Stopping after the first session leaves new unmuted
                # sessions at full volume, causing clipping when gain=100.

        if not found:
            logger.warning(
                "[HUB-MUTE-DIAG] mute_session: NO SESSION FOUND for pid=%d " "(all pids: %s)",
                pid,
                all_pids,
            )
            logger.warning("No audio session found for PID %d", pid)
        return found

    except ImportError:
        logger.warning("pycaw not available; cannot suppress PID %d", pid)
        return False
    except Exception:
        logger.exception("Failed to suppress PID %d", pid)
        return False


def unmute_session(pid: int) -> bool:
    """Restore full audio for the given PID.

    Resets volume to 1.0 and ensures unmuted.

    Args:
        pid: Process ID whose audio session should be restored.

    Returns:
        True if the session was found and restored, False otherwise.
    """
    import traceback as _tb

    _ensure_com_mta()

    # Capture caller for diagnostics (2 frames up: unmute_session → caller → ...)
    _caller = "unknown"
    try:
        _stack = _tb.extract_stack()
        # Walk up past unmute_session itself
        _relevant = [f for f in _stack if "hub_audio_controller" not in f.filename]
        if _relevant:
            _f = _relevant[-1]
            _caller = "%s:%d %s" % (_f.filename.split("\\")[-1], _f.lineno, _f.name)
    except Exception:
        logger.debug("Failed to resolve caller for unmute_session diagnostics", exc_info=True)

    logger.warning(
        "[HUB-MUTE-DIAG] unmute_session(pid=%d) called by %s",
        pid,
        _caller,
    )

    try:
        from pycaw.pycaw import AudioUtilities

        for session in AudioUtilities.GetAllSessions():
            if session.ProcessId == pid:
                session.SimpleAudioVolume.SetMasterVolume(1.0, None)
                session.SimpleAudioVolume.SetMute(0, None)
                actual_vol = session.SimpleAudioVolume.GetMasterVolume()
                logger.warning(
                    "[HUB-MUTE-DIAG] unmute_session: RESTORED pid=%d " "readback=%.4f",
                    pid,
                    actual_vol,
                )
                logger.info("Restored audio session for PID %d (volume=1.0)", pid)
                return True

        logger.warning("No audio session found for PID %d to restore", pid)
        return False

    except ImportError:
        logger.warning("pycaw not available; cannot restore PID %d", pid)
        return False
    except Exception:
        logger.exception("Failed to restore PID %d", pid)
        return False


def mute_self_session() -> int:
    """Suppress Viola's own audio session to QUIET_VOLUME.

    Tries os.getpid() first (single-process mode).  If no session is found,
    falls back to find_audio_child_pid() — needed when the Qt frontend runs
    as a separate subprocess (two-process mode) and holds the audio session.
    """
    if mute_session(os.getpid()):
        return 1
    audio_pid = find_audio_child_pid()
    if audio_pid is not None:
        return int(mute_session(audio_pid))
    return 0


def unmute_self_session() -> int:
    """Restore Viola's own audio session to full volume.

    Mirrors mute_self_session(): tries os.getpid() first, then the active
    audio child so both single-process and two-process modes are covered.
    """
    result = int(unmute_session(os.getpid()))
    audio_pid = find_audio_child_pid()
    if audio_pid is not None and audio_pid != os.getpid():
        result += int(unmute_session(audio_pid))
    return result


def mute_all_child_sessions() -> int:
    """Suppress ALL Viola-related audio sessions to QUIET_VOLUME.

    Mutes audio sessions for the main process AND all child processes.
    The main process is included because --disable-features=AudioServiceOutOfProcess
    keeps the audio session on Viola's PID rather than a child renderer.

    Returns:
        Number of sessions suppressed.
    """
    _ensure_com_mta()

    try:
        from pycaw.pycaw import AudioUtilities
    except (ImportError, OSError):
        return 0

    candidate_pids = _get_candidate_pids()
    if not candidate_pids:
        return 0

    try:
        count = 0
        for session in AudioUtilities.GetAllSessions():
            if session.ProcessId in candidate_pids:
                try:
                    session.SimpleAudioVolume.SetMute(0, None)
                    session.SimpleAudioVolume.SetMasterVolume(QUIET_VOLUME, None)
                    count += 1
                except Exception:
                    logger.debug("Failed to set volume on audio session pid=%d", session.ProcessId, exc_info=True)

        if count:
            logger.info("Muted %d audio session(s) to %.3f", count, QUIET_VOLUME)
        return count

    except Exception:
        logger.exception("Error in mute_all_child_sessions")
        return 0


def unmute_all_child_sessions() -> int:
    """Restore full volume for ALL Viola-related audio sessions.

    Restores the main process AND all child processes + external PIDs
    (mirrors mute_all_child_sessions).

    Returns:
        Number of sessions restored.
    """
    _ensure_com_mta()

    try:
        from pycaw.pycaw import AudioUtilities
    except (ImportError, OSError):
        return 0

    candidate_pids = _get_candidate_pids()
    if not candidate_pids:
        return 0

    try:
        count = 0
        for session in AudioUtilities.GetAllSessions():
            if session.ProcessId in candidate_pids:
                try:
                    session.SimpleAudioVolume.SetMasterVolume(1.0, None)
                    session.SimpleAudioVolume.SetMute(0, None)
                    count += 1
                except Exception:
                    logger.debug("Failed to restore volume on audio session pid=%d", session.ProcessId, exc_info=True)

        if count:
            logger.info("Restored %d audio session(s) to full volume", count)
        return count

    except Exception:
        logger.exception("Error in unmute_all_child_sessions")
        return 0


__all__ = [
    "QUIET_VOLUME",
    "exclude_pid",
    "find_all_audio_pids",
    "find_audio_child_pid",
    "include_pid",
    "mute_all_child_sessions",
    "mute_self_session",
    "mute_session",
    "register_external_audio_pid",
    "unmute_all_child_sessions",
    "unmute_self_session",
    "unmute_session",
    "unregister_external_audio_pid",
]
