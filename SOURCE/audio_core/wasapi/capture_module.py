"""
WASAPI Loopback Capture Module
==============================

Provides WASAPI loopback audio capture using PyAudioWPatch.
This captures system audio output for AEC reference.

ENDPOINT VOLUME COMPENSATION
----------------------------
On an output endpoint whose volume is applied in software (no hardware volume
control, which is most USB headsets and many onboard codecs), WASAPI loopback
hands back the mix *after* the Windows master volume. Measured on the dev box
(Amazonbasics210 USB headset, software volume, master volume at 0): a -23 dBFS
test tone came back at about -97 dBFS (2 LSB peaks); an earlier capture at a
low non-zero volume was ~44 dB under the played level. Music played at a low
Windows volume into an amplified speaker is therefore loud in the room but
reaches the echo canceller tens of dB down, under its -60 dBFS "reference is
silent" floor (``aec_viola._ref_threshold``), so the canceller bypasses (and
resets) instead of removing the music, and the wake word hears the song.

So while capturing, this module reads the endpoint volume about once a second
and scales the loopback back up to the pre-volume level, capped at
``MAX_LOOPBACK_COMPENSATION_DB``. That makes a software-volume endpoint look
like a hardware-volume one, where loopback is already pre-volume. It never
changes the system volume. Any failure (no pycaw, COM error, a loopback that is
not the default endpoint) leaves the gain at 1.0, which is the old behaviour.
"""

from __future__ import annotations

import platform
import threading
from collections.abc import Callable
from typing import cast

import numpy as np

from audio_core.portaudio_guard import PORTAUDIO_LOCK, open_stream, terminate_portaudio
from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_16K, SAMPLE_RATE_48K
from core.logging_config import get_logger

logger = get_logger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# Try to import PyAudioWPatch for native WASAPI loopback
_PYAUDIOWPATCH_AVAILABLE = False
try:
    if IS_WINDOWS:
        import pyaudiowpatch as pyaudio

        _PYAUDIOWPATCH_AVAILABLE = True
except ImportError:
    pyaudio = None


# --- Endpoint volume compensation (see module docstring) ---
#: Largest gain applied to undo the endpoint volume. 40 dB covers the low end
#: of the Windows slider people actually use with amplified speakers without
#: turning quantization noise into a loud fake reference.
MAX_LOOPBACK_COMPENSATION_DB = 40.0
#: At or below this master level the speakers are effectively silent, so there
#: is no echo to cancel; the loopback is left as is rather than amplifying noise.
ENDPOINT_SILENT_DB = -60.0
#: A loopback block quieter than this (int16 RMS, before gain) is passed through
#: unamplified: it is dither or digital silence, not playback.
LOOPBACK_NOISE_FLOOR_RMS = 2.0
#: How often the endpoint volume is re-read while capturing.
ENDPOINT_VOLUME_POLL_S = 1.0
#: IAudioEndpointVolume::QueryHardwareSupport bit for hardware volume control.
_ENDPOINT_HARDWARE_SUPPORT_VOLUME = 0x1


def loopback_compensation_gain(
    master_db: float | None,
    *,
    muted: bool = False,
    hardware_volume: bool = False,
) -> float:
    """Linear gain that restores a loopback captured after a software endpoint volume.

    Returns 1.0 (no change) when the level is unknown, the endpoint is muted or
    effectively silent, the volume is applied in hardware (loopback is already
    pre-volume there), or the volume is at or above 0 dB.
    """
    if master_db is None or muted or hardware_volume:
        return 1.0
    try:
        db = float(master_db)
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(db) or db >= 0.0 or db <= ENDPOINT_SILENT_DB:
        return 1.0
    return float(10.0 ** (min(-db, MAX_LOOPBACK_COMPENSATION_DB) / 20.0))


def apply_loopback_gain(pcm_bytes: bytes, gain: float) -> bytes:
    """Scale int16 loopback PCM by ``gain`` with clipping; noise-floor blocks pass unchanged."""
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if gain == 1.0 or usable <= 0:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes[:usable], dtype=np.int16).astype(np.float32)
    if float(np.sqrt(np.mean(samples * samples))) < LOOPBACK_NOISE_FLOOR_RMS:
        return pcm_bytes
    return np.clip(np.rint(samples * gain), -32768, 32767).astype(np.int16).tobytes()


def _read_default_endpoint_volume() -> tuple[str, float, bool, bool] | None:
    """Return ``(friendly_name, master_db, muted, hardware_volume)`` for the default render endpoint.

    The caller must have initialized COM on this thread. Returns None when
    pycaw/comtypes are missing or the endpoint cannot be read.
    """
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        return None
    try:
        device = AudioUtilities.GetSpeakers()
        endpoint = getattr(device, "EndpointVolume", None)
        if endpoint is None:
            activate = getattr(device, "Activate", None) or getattr(getattr(device, "_dev", None), "Activate", None)
            if not callable(activate):
                return None
            endpoint = activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
        name = str(getattr(device, "FriendlyName", "") or "")
        master_db = float(endpoint.GetMasterVolumeLevel())
        muted = bool(endpoint.GetMute())
        hardware_volume = bool(int(endpoint.QueryHardwareSupport()) & _ENDPOINT_HARDWARE_SUPPORT_VOLUME)
        return name, master_db, muted, hardware_volume
    except Exception as exc:  # noqa: BLE001, RUF100 - COM failures must leave the reference untouched
        logger.debug("Endpoint volume read failed: %s", exc)
        return None


class NativeWasapiCaptureModule:
    """
    WASAPI loopback capture using PyAudioWPatch.

    Captures system audio output (what you hear) from the default
    or specified output device and provides it via callback for
    AEC reference processing.
    """

    def __init__(self):
        self._stream = None
        self._pyaudio: pyaudio.PyAudio | None = None
        self._running = False
        self._callback: Callable[[bytes], None] | None = None
        self._lock = threading.Lock()
        self._sample_rate = SAMPLE_RATE_16K
        self._channels = 2
        self._loopback_device_info: dict | None = None
        self._callback_error_count = 0
        self._callback_error_logged = False
        # Endpoint volume compensation (see module docstring)
        self._loopback_gain = 1.0
        self._volume_stop = threading.Event()
        self._volume_thread: threading.Thread | None = None

    def start(self, config, callback: Callable[[bytes], None]) -> bool:
        """
        Start WASAPI loopback capture.

        Args:
            config: Config object with sample_rate attribute
            callback: Function to call with PCM bytes

        Returns:
            True if started successfully
        """
        if not IS_WINDOWS:
            logger.warning("WASAPI loopback only available on Windows")
            return False

        if not _PYAUDIOWPATCH_AVAILABLE:
            logger.warning("PyAudioWPatch not available - install with: pip install pyaudiowpatch")
            return self._fallback_sounddevice_start(config, callback)

        with self._lock:
            if self._running:
                return True

            self._callback = callback
            self._sample_rate = getattr(config, "sample_rate", SAMPLE_RATE_16K)

            try:
                with PORTAUDIO_LOCK:
                    self._pyaudio = pyaudio.PyAudio()

                # Find loopback device for default output
                loopback_device = self._find_loopback_for_default_output()
                if loopback_device is None:
                    logger.error("No WASAPI loopback device found")
                    terminate_portaudio(self._pyaudio)
                    self._pyaudio = None
                    return False

                self._loopback_device_info = loopback_device
                # Cast values from dict to expected types (dict values are typed as object)
                sample_rate_val = loopback_device.get("defaultSampleRate")
                device_sample_rate = (
                    int(cast(float, sample_rate_val)) if sample_rate_val is not None else SAMPLE_RATE_48K
                )
                self._device_sample_rate = device_sample_rate  # Store for adapter to read
                channels_val = loopback_device.get("maxInputChannels")
                self._channels = int(cast(float, channels_val)) if channels_val is not None else 2

                logger.info(
                    "Starting WASAPI loopback capture: device=%s, rate=%d, channels=%d",
                    loopback_device.get("name", "unknown"),
                    device_sample_rate,
                    self._channels,
                )

                def audio_callback(in_data, frame_count, time_info, status):
                    self._dispatch_pcm_callback(in_data)
                    return (None, pyaudio.paContinue)

                self._stream = open_stream(
                    self._pyaudio,
                    format=pyaudio.paInt16,
                    channels=self._channels,
                    rate=device_sample_rate,
                    input=True,
                    input_device_index=loopback_device["index"],
                    frames_per_buffer=int(device_sample_rate * 0.01),  # 10ms blocks
                    stream_callback=audio_callback,
                )
                self._stream.start_stream()
                self._running = True
                self._start_volume_watch(str(loopback_device.get("name", "")))

                logger.info("WASAPI loopback capture started successfully (native)")
                return True

            except Exception as e:
                logger.error("Failed to start WASAPI loopback: %s", e)
                if self._pyaudio:
                    terminate_portaudio(self._pyaudio)
                    self._pyaudio = None
                self._running = False
                return False

    def stop(self) -> None:
        """Stop WASAPI loopback capture."""
        self._stop_volume_watch()
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception as e:
                    logger.debug("Error stopping WASAPI stream: %s", e)
                finally:
                    self._stream = None

            if self._pyaudio is not None:
                try:
                    # Drains any guarded stream on this instance before
                    # Pa_Terminate, which would otherwise free it from here (#4650).
                    terminate_portaudio(self._pyaudio)
                except Exception as e:
                    logger.debug("Error terminating PyAudio: %s", e)
                finally:
                    self._pyaudio = None

            self._running = False
            self._callback = None
            self._loopback_device_info = None

    def _dispatch_pcm_callback(self, pcm_bytes: bytes | None) -> None:
        """Dispatch loopback PCM without letting errors escape the native callback."""
        callback = self._callback
        if callback is None or not pcm_bytes:
            return
        try:
            gain = self._loopback_gain
            if gain != 1.0:
                pcm_bytes = apply_loopback_gain(pcm_bytes, gain)
            callback(pcm_bytes)
        except Exception as exc:  # noqa: BLE001, RUF100 - PortAudio callback safety boundary
            self._record_callback_error("dispatch", exc)

    # ------------------------------------------------------------------ #
    # Endpoint volume compensation                                         #
    # ------------------------------------------------------------------ #

    def _update_loopback_gain(self, loopback_name: str, reading: tuple[str, float, bool, bool] | None) -> float:
        """Set the loopback gain from one endpoint-volume reading and return it.

        Only compensates when the reading is for the endpoint this loopback
        captures (loopback names are ``"{endpoint name} [Loopback]"``); a
        fallback loopback on another device keeps gain 1.0.
        """
        gain = 1.0
        if reading is not None:
            name, master_db, muted, hardware_volume = reading
            if name and loopback_name.startswith(name):
                gain = loopback_compensation_gain(master_db, muted=muted, hardware_volume=hardware_volume)
        if abs(gain - self._loopback_gain) > 1e-3:
            logger.info(
                "[WASAPI] Loopback endpoint-volume compensation %.1f dB -> %.1f dB (reading=%s)",
                20.0 * np.log10(self._loopback_gain),
                20.0 * np.log10(gain),
                reading,
            )
        self._loopback_gain = gain
        return gain

    def _start_volume_watch(self, loopback_name: str) -> None:
        """Poll the endpoint volume on a daemon thread while capture runs."""
        if self._volume_thread is not None and self._volume_thread.is_alive():
            return
        self._volume_stop.clear()

        def _watch() -> None:
            try:
                from comtypes import CoInitialize, CoUninitialize
            except ImportError:
                return
            try:
                CoInitialize()
            except OSError as exc:
                logger.debug("COM init failed for loopback volume watch: %s", exc)
                return
            try:
                while not self._volume_stop.is_set():
                    self._update_loopback_gain(loopback_name, _read_default_endpoint_volume())
                    self._volume_stop.wait(ENDPOINT_VOLUME_POLL_S)
            finally:
                self._loopback_gain = 1.0
                try:
                    CoUninitialize()
                except (OSError, RuntimeError):
                    logger.debug("COM uninitialization failed after loopback volume watch")

        self._volume_thread = threading.Thread(target=_watch, name="wasapi-loopback-volume", daemon=True)
        self._volume_thread.start()

    def _stop_volume_watch(self) -> None:
        self._volume_stop.set()
        thread = self._volume_thread
        self._volume_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._loopback_gain = 1.0

    def _record_callback_error(self, source: str, exc: BaseException) -> None:
        self._callback_error_count += 1
        if self._callback_error_logged and self._callback_error_count % 100 != 0:
            return
        self._callback_error_logged = True
        logger.warning(
            "WASAPI loopback callback %s failed; callback_error_count=%d: %s",
            source,
            self._callback_error_count,
            exc,
        )

    def _find_loopback_for_default_output(self) -> dict[str, object] | None:
        """
        Find the WASAPI loopback device for the default output.

        Uses PyAudioWPatch's loopback device generator to find the
        loopback endpoint matching the system's default audio output.

        Returns:
            Device info dict or None if not found.
        """
        if not self._pyaudio:
            return None

        try:
            # Get WASAPI host API info
            wasapi_info = self._pyaudio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_output_idx = wasapi_info.get("defaultOutputDevice")

            if default_output_idx is None or default_output_idx < 0:
                logger.warning("No default WASAPI output device found")
                return None

            # Get default output device info
            default_output = self._pyaudio.get_device_info_by_index(default_output_idx)
            default_name = default_output.get("name", "")

            logger.debug("Looking for loopback matching default output: %s", default_name)

            # Match on the FULL device name, not just the prefix.
            # Loopback names are "{device_name} [Loopback]", so
            # "Headphones (Amazonbasics210) [Loopback]".startswith("Headphones (Amazonbasics210)")
            # is True, but "Headphones (High Definition Audio Device) [Loopback]" is False.
            for loopback in self._pyaudio.get_loopback_device_info_generator():
                loopback_name = loopback.get("name", "")
                if loopback_name.startswith(default_name):
                    logger.info(
                        "[WASAPI] Selected loopback: %s (index %d)",
                        loopback_name,
                        loopback["index"],
                    )
                    return cast(dict[str, object], loopback)

            # Fallback: return first loopback device if no match
            logger.warning("No loopback matching '%s', using first available", default_name)
            for loopback in self._pyaudio.get_loopback_device_info_generator():
                logger.debug("Using fallback loopback: %s", loopback.get("name"))
                return cast(dict[str, object], loopback)

            return None

        except Exception as e:
            logger.error("Error finding loopback device: %s", e)
            return None

    def _fallback_sounddevice_start(self, config, callback: Callable[[bytes], None]) -> bool:
        """
        Fallback to sounddevice if PyAudioWPatch is not available.

        This has limited functionality - only works if Stereo Mix or
        similar virtual audio device is enabled.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            logger.warning("sounddevice not available for fallback loopback capture")
            return False

        with self._lock:
            if self._running:
                return True

            self._callback = callback
            self._sample_rate = getattr(config, "sample_rate", SAMPLE_RATE_16K)

            # Try legacy pattern-matching approach
            loopback_device = self._find_loopback_device_legacy()
            if loopback_device is None:
                logger.warning(
                    "No WASAPI loopback device found - AEC unavailable. "
                    "Install pyaudiowpatch for native loopback support."
                )
                return False

            try:
                device_info = sd.query_devices(loopback_device)
                device_sample_rate = int(device_info.get("default_samplerate", SAMPLE_RATE_48K))
                self._channels = int(device_info.get("max_input_channels", 2))

                logger.info(
                    "Starting WASAPI loopback capture (fallback): device=%s, rate=%d",
                    device_info.get("name", "unknown"),
                    device_sample_rate,
                )

                def audio_callback(indata, frames, time_info, status):
                    if indata is not None:
                        try:
                            int16_data = (indata * AUDIO_INT16_MAX).astype(np.int16)
                        except Exception as exc:  # noqa: BLE001, RUF100 - sounddevice callback safety boundary
                            self._record_callback_error("float32_conversion", exc)
                            return
                        self._dispatch_pcm_callback(int16_data.tobytes())

                self._stream = sd.InputStream(
                    device=loopback_device,
                    samplerate=device_sample_rate,
                    channels=self._channels,
                    dtype="float32",
                    callback=audio_callback,
                    blocksize=int(device_sample_rate * 0.01),
                )
                self._stream.start()
                self._running = True

                logger.info("WASAPI loopback capture started (fallback mode)")
                return True

            except Exception as e:
                logger.error("Failed to start fallback loopback: %s", e)
                self._running = False
                return False

    def _find_loopback_device_legacy(self) -> int | None:
        """
        Legacy loopback device finder using pattern matching.

        Only used as fallback when PyAudioWPatch is not available.
        """
        try:
            import sounddevice as sd

            from audio_core.portaudio_guard import sounddevice_guard

            with sounddevice_guard():
                devices = sd.query_devices()

            # Look for loopback device patterns
            for i, dev in enumerate(devices):
                name = dev.get("name", "").lower()
                if "loopback" in name or "wasapi" in name.lower():
                    if dev.get("max_input_channels", 0) > 0:
                        return i

            # Fallback: stereo mix, etc.
            for i, dev in enumerate(devices):
                name = dev.get("name", "").lower()
                if any(term in name for term in ["stereo mix", "what you hear", "wave out"]):
                    if dev.get("max_input_channels", 0) > 0:
                        return i

            return None

        except Exception as e:
            logger.debug("Error finding legacy loopback device: %s", e)
            return None

    @property
    def is_running(self) -> bool:
        """Check if capture is running."""
        return self._running

    @property
    def callback_error_count(self) -> int:
        """Return native callback errors swallowed at the PortAudio boundary."""
        return self._callback_error_count

    @property
    def loopback_device_name(self) -> str | None:
        """Get the name of the active loopback device."""
        if self._loopback_device_info:
            return self._loopback_device_info.get("name")
        return None


def get_available_loopback_devices() -> list[dict]:
    """
    Get list of available WASAPI loopback devices.

    Returns:
        List of device info dicts with 'index', 'name', 'defaultSampleRate', 'maxInputChannels'
    """
    if not IS_WINDOWS or not _PYAUDIOWPATCH_AVAILABLE:
        return []

    devices = []
    try:
        # Hold the process-wide PortAudio lock across create -> enumerate ->
        # terminate (pyaudiowpatch can't use portaudio_instance(), which imports
        # plain pyaudio). Prevents another thread tearing the table down mid-walk.
        with PORTAUDIO_LOCK:
            p = pyaudio.PyAudio()
            try:
                for loopback in p.get_loopback_device_info_generator():
                    devices.append(
                        {
                            "index": loopback["index"],
                            "name": loopback["name"],
                            "defaultSampleRate": loopback["defaultSampleRate"],
                            "maxInputChannels": loopback["maxInputChannels"],
                        }
                    )
            finally:
                p.terminate()
    except Exception as e:
        logger.debug("Error listing loopback devices: %s", e)

    return devices


__all__ = [
    "ENDPOINT_SILENT_DB",
    "IS_WINDOWS",
    "LOOPBACK_NOISE_FLOOR_RMS",
    "MAX_LOOPBACK_COMPENSATION_DB",
    "_PYAUDIOWPATCH_AVAILABLE",
    "NativeWasapiCaptureModule",
    "apply_loopback_gain",
    "get_available_loopback_devices",
    "loopback_compensation_gain",
]
