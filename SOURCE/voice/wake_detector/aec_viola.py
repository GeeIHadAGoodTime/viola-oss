"""
ViolaAEC - Frequency-Domain Adaptive Filter (FDAF) with Double-Talk Detection
==============================================================================

A pure-numpy FDAF implementation using the overlap-save method.
FDAF converges 10-100x faster than time-domain NLMS by decorrelating
input through FFT, allowing each frequency bin to converge independently.

Key advantages over time-domain NLMS:
- O(N log N) complexity vs O(N^2) for long filters
- Per-frequency normalization handles colored noise better
- High-energy bins (music fundamentals) converge fast
- Much higher step size (mu) tolerated

Algorithm (Overlap-Save):
1. Maintain reference buffer of size N (FFT size)
2. X = fft(ref_buffer) → reference spectrum
3. Y = X * W → estimated echo spectrum (W = filter in freq domain)
4. y = ifft(Y).real[-L:] → estimated echo (last L samples)
5. e = mic - y → error/output
6. If NOT DTD: update W using normalized FDAF rule

DTD Output Modes:
- waveform: standard time-domain subtraction
- spectral: spectral subtraction preserving mic phase
- partial: reduced cancellation (output = mic - dtd_gain * echo)
- fdaf_irm: FDAF-estimated IRM (Ideal Ratio Mask) in STFT domain
  Uses the FDAF's echo estimate to build a spectral mask instead of
  time-domain subtraction. Combines FDAF's adaptive estimation with
  the proven-superior IRM application method.

Reference:
- Haykin, "Adaptive Filter Theory", Ch. 11 (Frequency-Domain Adaptive Filters)
- Shynk, "Frequency-domain and multirate adaptive filtering", IEEE SP Magazine

Author: Claude for Viola wake word project
"""

from __future__ import annotations

from collections import deque

import numpy as np
import numpy.typing as npt
from scipy.signal import get_window

from core.logging_config import get_logger

logger = get_logger(__name__)


class ViolaAEC:
    """
    Frequency-Domain Adaptive Filter (FDAF) with Frame-Level DTD.

    FDAF uses FFT-based convolution and per-frequency-bin updates,
    converging much faster than time-domain NLMS for long filters.

    Args:
        frame_size: Samples per frame (default 160 = 10ms at 16kHz)
        sample_rate: Audio sample rate (default 16000)
        filter_length_ms: Adaptive filter length in milliseconds
        mu: FDAF step size (can be much higher than NLMS, 0.1-1.0)
        dtd_threshold: Error-to-baseline ratio threshold for DTD
        dtd_holdoff_frames: Frames to stay frozen after DTD fires
        warmup_frames: Frames of warmup before DTD activates
        dtd_mode: DTD output mode - "waveform" (default), "spectral", "partial", or "fdaf_irm"
            waveform: output = mic - estimated_echo (standard)
            spectral: spectral subtraction preserving mic phase
            partial: output = mic - (dtd_gain * estimated_echo), reduced cancellation
            fdaf_irm: FDAF-estimated IRM (Ideal Ratio Mask) in STFT domain
        spectral_alpha: Over-subtraction factor for spectral mode (1.0 = exact)
        dtd_gain: Cancellation gain during DTD for partial mode (0.5 = half cancellation)
        mask_floor: Minimum mask value for fdaf_irm mode (0.0 = aggressive, higher = more conservative)
        mask_all_frames: If True, apply spectral mask to ALL frames (not just DTD) for fdaf_irm mode
        echo_scale: Scale factor for the echo estimate before subtraction (1.0 = use raw estimate).
            If the FDAF under-estimates echo by factor alpha (e.g. 0.70), set echo_scale = 1/alpha
            (e.g. 1.43) to correct the magnitude. Does NOT affect filter adaptation — only the output.
    """

    # STFT constants matching violawake/config.py
    STFT_WIN_LENGTH = 400  # 25ms at 16kHz
    STFT_HOP_LENGTH = 160  # 10ms at 16kHz (same as frame_size)
    STFT_N_FFT = 512

    def __init__(
        self,
        frame_size: int = 160,
        sample_rate: int = 16000,
        filter_length_ms: int = 150,  # 2400 taps - capture more room reflections
        mu: float = 0.1,  # Best balance of convergence speed and stability
        dtd_threshold: float = 10.0,  # Error must be 10x baseline for DTD (balanced)
        dtd_holdoff_frames: int = 50,  # 500ms holdoff after DTD
        warmup_frames: int = 1000,  # ~10 seconds warmup (matches test setup)
        dtd_mode: str = "waveform",  # "waveform", "spectral", "partial", or "fdaf_irm"
        spectral_alpha: float = 1.0,  # Over-subtraction factor
        dtd_gain: float = 0.8,  # Reduced cancellation during DTD preserves more speech patterns
        mask_floor: float = 0.0,  # Minimum mask value for fdaf_irm (0.0 = aggressive)
        mask_all_frames: bool = False,  # Apply mask to all frames, not just DTD
        dtd_leak: float = 0.0,  # Leaky DTD: allow slow adaptation during speech (0.0 = full freeze)
        echo_scale: float = 1.0,  # Scale factor for echo estimate (1.0 = unscaled, optimal with baseline fix)
    ) -> None:
        self.frame_size = frame_size
        self.sample_rate = sample_rate
        self.mu = mu
        self.dtd_threshold = dtd_threshold
        self.dtd_holdoff_frames = dtd_holdoff_frames
        self.warmup_frames = warmup_frames
        self.dtd_mode = dtd_mode
        self.spectral_alpha = spectral_alpha
        self.dtd_gain = dtd_gain
        self.mask_floor = mask_floor
        self.mask_all_frames = mask_all_frames
        self.dtd_leak = dtd_leak
        self.echo_scale = echo_scale

        # --- ERLE / diagnostics running averages ---
        self._erle_avg: float = 0.0
        self._erle_instant: float = 0.0
        self._mic_rms: float = 0.0
        self._output_rms: float = 0.0
        self._ref_rms: float = 0.0
        self._erle_alpha: float = 0.01  # EMA smoothing for ERLE

        # ERLE 60-second rolling window (6000 frames at 100 FPS)
        self._erle_history: deque[float] = deque(maxlen=6000)

        # Last computed error-to-baseline ratio (updated each frame)
        self._last_error_to_baseline: float = 0.0

        # DTD ratio: rolling window of last 500 frames
        self._dtd_window_size: int = 500
        self._dtd_window: list[bool] = []

        # Reference activity: fraction of last 100 frames with ref_power > threshold
        self._ref_window_size: int = 100
        self._ref_threshold: float = 1e-6  # normalized power threshold
        self._ref_window: list[bool] = []
        self._silent_ref_bypass_count = 0
        self._silent_ref_bypass_active = False

        # Filter length in samples
        self.filter_length = int(sample_rate * filter_length_ms / 1000)

        # FFT size: must be >= 2 * filter_length and power of 2
        # Using 2 * filter_length gives overlap-save with 50% overlap
        self.fft_size = 1
        while self.fft_size < 2 * self.filter_length:
            self.fft_size *= 2
        # Ensure FFT size is at least 2 * frame_size for proper overlap
        while self.fft_size < 2 * frame_size:
            self.fft_size *= 2

        # Adaptive filter in frequency domain (complex)
        self._W = np.zeros(self.fft_size, dtype=np.complex128)

        # Reference signal buffer (size = fft_size)
        self._ref_buffer = np.zeros(self.fft_size, dtype=np.float64)

        # Power spectrum estimate for normalization (smoothed)
        self._power_spectrum = np.ones(self.fft_size, dtype=np.float64) * 1e-6

        # Regularization per frequency bin
        self._eps = 1e-3

        # Power spectrum smoothing factor
        self._power_alpha = 0.1

        # DTD state
        self._dtd_holdoff_counter = 0
        self._baseline_error_power = 1e-6
        self._baseline_alpha = 0.01  # Faster adaptation for FDAF

        # DTD death-spiral prevention (unconditional baseline leak + safety valve)
        self._dtd_baseline_leak = 0.1  # fraction of alpha used during DTD
        self._max_continuous_dtd = 500  # frames before force-release (~5s at 100Hz)
        self._dtd_force_release = 50  # force-release window frames (~500ms)
        self._continuous_dtd_counter = 0
        self._dtd_force_release_remaining = 0

        # Statistics tracking
        self._frame_count = 0
        self._dtd_frame_count = 0

        # === FDAF-IRM specific buffers ===
        if dtd_mode == "fdaf_irm":
            # STFT analysis buffers (WIN_LENGTH samples each)
            self._mic_stft_buf = np.zeros(self.STFT_WIN_LENGTH, dtype=np.float64)
            self._echo_stft_buf = np.zeros(self.STFT_WIN_LENGTH, dtype=np.float64)

            # Hann window for STFT analysis
            self._hann_window = get_window("hann", self.STFT_WIN_LENGTH, fftbins=False)

            # IRM epsilon for numerical stability
            self._irm_eps = 1e-10

    def _bypass_silent_reference(
        self,
        mic_frame: npt.NDArray[np.int16],
        mic: npt.NDArray[np.float64],
        ref_power: float,
    ) -> npt.NDArray[np.int16]:
        """Pass through mic audio when there is no far-end signal to cancel."""
        mic_power = float(np.mean(mic**2))
        mic_rms = float(np.sqrt(mic_power))

        self._mic_rms = mic_rms
        self._output_rms = mic_rms
        self._ref_rms = float(np.sqrt(ref_power))
        self._erle_instant = 0.0
        self._erle_avg = (1.0 - self._erle_alpha) * self._erle_avg
        self._erle_history.append(0.0)
        self._last_error_to_baseline = 0.0

        self._ref_window.append(False)
        if len(self._ref_window) > self._ref_window_size:
            self._ref_window.pop(0)
        self._dtd_window.append(False)
        if len(self._dtd_window) > self._dtd_window_size:
            self._dtd_window.pop(0)

        self._ref_buffer.fill(0.0)
        if hasattr(self, "_echo_stft_buf"):
            self._echo_stft_buf.fill(0.0)

        self._baseline_error_power = 1e-6
        self._dtd_holdoff_counter = 0
        self._continuous_dtd_counter = 0
        self._dtd_force_release_remaining = 0
        self._frame_count = 0
        self._silent_ref_bypass_count += 1
        self._silent_ref_bypass_active = True
        return mic_frame

    def _process_fdaf_irm(
        self,
        mic: npt.NDArray[np.float64],
        estimated_echo: npt.NDArray[np.float64],
    ) -> npt.NDArray[np.float64]:
        """
        Apply FDAF-estimated IRM (Ideal Ratio Mask) using scipy STFT/iSTFT.

        Uses the FDAF's echo estimate to build a spectral mask:
        1. Buffer mic and echo samples for STFT window
        2. When buffer is full, compute STFT with scipy
        3. Compute IRM mask from echo estimate
        4. Apply mask and compute iSTFT
        5. Use overlap-add for continuous output

        The key insight: use scipy's STFT/iSTFT for proper windowing and
        overlap handling, matching the oracle implementation.

        Args:
            mic: Current mic frame (frame_size samples, float64)
            estimated_echo: FDAF echo estimate for this frame (frame_size samples)

        Returns:
            Processed output frame (frame_size samples, float64)
        """
        from scipy import signal as scipy_signal

        L = self.frame_size
        WIN = self.STFT_WIN_LENGTH  # 400
        HOP = L  # 160 = STFT_HOP_LENGTH

        # === STEP 1: Update STFT buffers ===
        self._mic_stft_buf[:-L] = self._mic_stft_buf[L:]
        self._mic_stft_buf[-L:] = mic

        self._echo_stft_buf[:-L] = self._echo_stft_buf[L:]
        self._echo_stft_buf[-L:] = estimated_echo

        # During first frames, fall back to time-domain
        if self._frame_count < (WIN // L):
            return mic - estimated_echo

        # === STEP 2: Compute STFT using scipy ===
        # Process the buffered samples with proper windowing
        _, _, Mic = scipy_signal.stft(
            self._mic_stft_buf,
            fs=self.sample_rate,
            nperseg=WIN,
            noverlap=WIN - HOP,
            boundary=None,
            padded=False,
        )
        _, _, Echo = scipy_signal.stft(
            self._echo_stft_buf,
            fs=self.sample_rate,
            nperseg=WIN,
            noverlap=WIN - HOP,
            boundary=None,
            padded=False,
        )

        # === STEP 3: Compute IRM mask ===
        mic_power = np.abs(Mic) ** 2
        echo_power = np.abs(Echo) ** 2

        # Estimate speech power (accounting for mic = speech + echo)
        estimated_speech_power = np.maximum(mic_power - echo_power, 0)

        # Wiener-style mask
        mask = estimated_speech_power / (estimated_speech_power + echo_power + self._irm_eps)
        mask = np.clip(mask, self.mask_floor, 1.0)

        # === STEP 4: Apply mask ===
        Masked = Mic * mask

        # === STEP 5: Inverse STFT ===
        _, output_full = scipy_signal.istft(Masked, fs=self.sample_rate, nperseg=WIN, noverlap=WIN - HOP)

        # Return the last HOP samples (corresponding to current frame position)
        if len(output_full) >= L:
            return output_full[-L:]
        else:
            # Fallback if output is shorter than expected
            return mic - estimated_echo

    def process_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16],
    ) -> npt.NDArray[np.int16]:
        """
        Process one frame of audio with FDAF echo cancellation.

        Args:
            mic_frame: int16 array, shape (frame_size,), microphone input
            ref_frame: int16 array, shape (frame_size,), loopback reference

        Returns:
            int16 array, shape (frame_size,), echo-cancelled output
        """
        L = self.frame_size
        N = self.fft_size

        # Convert to normalized float64 [-1, 1]
        mic = mic_frame.astype(np.float64) / 32768.0
        ref = ref_frame.astype(np.float64) / 32768.0
        ref_power = float(np.mean(ref**2))

        if ref_power <= self._ref_threshold:
            return self._bypass_silent_reference(mic_frame, mic, ref_power)

        if self._silent_ref_bypass_active:
            self.restart_warmup()
            self._silent_ref_bypass_active = False

        self._frame_count += 1

        # === STEP 1: Update reference buffer (shift left, add new frame) ===
        self._ref_buffer[:-L] = self._ref_buffer[L:]
        self._ref_buffer[-L:] = ref

        # === STEP 2: Compute reference spectrum ===
        X = np.fft.fft(self._ref_buffer)

        # === STEP 3: Compute estimated echo in frequency domain ===
        Y = X * self._W

        # === STEP 4: Convert to time domain (overlap-save: take last L samples) ===
        y_full = np.fft.ifft(Y).real
        estimated_echo = y_full[-L:]

        # === STEP 5: Compute error and output ===
        # filter_error: unscaled error for adaptive filter update (keeps convergence stable)
        # output: scaled echo removal for the returned signal
        filter_error = mic - estimated_echo
        output = mic - self.echo_scale * estimated_echo

        # === STEP 6: Frame-level DTD decision ===
        # DTD uses unscaled error so thresholds don't shift with echo_scale
        mic_power = np.mean(mic**2)
        error_power = np.mean(filter_error**2)
        _echo_power = np.mean(estimated_echo**2)
        output_power = np.mean(output**2)

        # --- ERLE diagnostics (lightweight running averages) ---
        self._mic_rms = float(np.sqrt(mic_power))
        self._output_rms = float(np.sqrt(output_power))
        self._ref_rms = float(np.sqrt(ref_power))

        # ERLE = 10*log10(mic_power / output_power) — only meaningful when ref active
        if mic_power > 1e-10 and output_power > 1e-10:
            self._erle_instant = float(10.0 * np.log10(mic_power / output_power))
        else:
            self._erle_instant = 0.0
        self._erle_avg = self._erle_alpha * self._erle_instant + (1.0 - self._erle_alpha) * self._erle_avg
        self._erle_history.append(self._erle_instant)

        # Track error-to-baseline ratio for diagnostics
        self._last_error_to_baseline = float(error_power / max(self._baseline_error_power, 1e-10))

        # Reference activity window
        self._ref_window.append(ref_power > self._ref_threshold)
        if len(self._ref_window) > self._ref_window_size:
            self._ref_window.pop(0)

        # During warmup, always adapt (no DTD)
        in_warmup = self._frame_count <= self.warmup_frames

        dtd_fires = False
        if not in_warmup:
            # DTD by error spike: error much larger than baseline
            dtd_by_error = (
                error_power > self.dtd_threshold * self._baseline_error_power and self._baseline_error_power > 1e-8
            )

            # NOTE: Removed dtd_by_mic - it triggers incorrectly when filter
            # hasn't fully converged (mic > 2*estimated_echo even during music-only)
            # Rely solely on error-based DTD which is more robust

            dtd_fires = dtd_by_error

        # --- Change 3: Re-anchor baseline at warmup exit ---
        # Give DTD a realistic starting baseline instead of one from silence
        if self._frame_count == self.warmup_frames:
            self._baseline_error_power = max(error_power, 1e-6)

        # --- Change 1: Unconditional baseline tracking ---
        # Normal alpha when NOT in DTD, 10x slower leak during DTD.
        # The slow leak lets baseline gradually rise toward actual error
        # power during sustained DTD, eventually releasing the lock.
        if in_warmup or (not dtd_fires and self._dtd_holdoff_counter == 0):
            alpha = self._baseline_alpha
        else:
            alpha = self._baseline_alpha * self._dtd_baseline_leak
        self._baseline_error_power = alpha * error_power + (1 - alpha) * self._baseline_error_power

        # Holdoff mechanism
        if dtd_fires:
            self._dtd_holdoff_counter = self.dtd_holdoff_frames

        dtd_active = self._dtd_holdoff_counter > 0

        # --- Change 2: Maximum continuous DTD safety valve ---
        if dtd_active:
            self._continuous_dtd_counter += 1
        else:
            self._continuous_dtd_counter = 0

        if self._dtd_force_release_remaining > 0:
            # In force-release window: override DTD to off
            dtd_active = False
            self._dtd_holdoff_counter = 0
            self._dtd_force_release_remaining -= 1
        elif self._continuous_dtd_counter >= self._max_continuous_dtd:
            # Max hold time reached: force-release DTD
            logger.warning(
                "[AEC] DTD max hold reached (%d frames), force-releasing",
                self._continuous_dtd_counter,
            )
            dtd_active = False
            self._dtd_holdoff_counter = 0
            self._baseline_error_power = max(error_power, 1e-6)
            self._continuous_dtd_counter = 0
            self._dtd_force_release_remaining = self._dtd_force_release

        # DTD ratio rolling window
        self._dtd_window.append(dtd_active)
        if len(self._dtd_window) > self._dtd_window_size:
            self._dtd_window.pop(0)

        if dtd_active:
            self._dtd_holdoff_counter -= 1
            self._dtd_frame_count += 1

            if self.dtd_mode == "spectral":
                # SPECTRAL SUBTRACTION: Remove echo magnitude, preserve mic phase
                # This preserves the spectral pattern better than waveform subtraction
                # because mel spectrograms are more sensitive to magnitude than phase

                # Use a frame-sized FFT for the current frame
                frame_fft_size = 256  # Power of 2 >= frame_size for efficiency
                while frame_fft_size < L:
                    frame_fft_size *= 2

                # Zero-pad mic and estimated echo to frame_fft_size
                mic_padded = np.zeros(frame_fft_size, dtype=np.float64)
                mic_padded[:L] = mic
                echo_padded = np.zeros(frame_fft_size, dtype=np.float64)
                echo_padded[:L] = estimated_echo

                # Compute spectra
                Mic_spec = np.fft.fft(mic_padded)
                Echo_spec = np.fft.fft(echo_padded)

                # Spectral subtraction: subtract magnitude, keep mic phase
                mic_mag = np.abs(Mic_spec)
                echo_mag = np.abs(Echo_spec)
                mic_phase = np.angle(Mic_spec)

                # Subtract echo magnitude with over-subtraction factor and echo_scale
                output_mag = np.maximum(mic_mag - self.spectral_alpha * self.echo_scale * echo_mag, 0.0)

                # Reconstruct with original mic phase
                Output_spec = output_mag * np.exp(1j * mic_phase)
                output_full = np.fft.ifft(Output_spec).real

                # Take first L samples
                output = output_full[:L]

            elif self.dtd_mode == "partial":
                # PARTIAL CANCELLATION: Reduce cancellation during DTD to preserve speech
                # output = mic - (dtd_gain * echo_scale * estimated_echo)
                # dtd_gain < 1.0 preserves more of the original mic signal
                # echo_scale corrects the magnitude under-estimation
                output = mic - self.dtd_gain * self.echo_scale * estimated_echo

            elif self.dtd_mode == "fdaf_irm":
                # FDAF-IRM: Use FDAF echo estimate to build spectral mask
                # This combines FDAF's adaptive estimation with IRM application method
                output = self._process_fdaf_irm(mic, self.echo_scale * estimated_echo)

            else:
                # WAVEFORM mode: Apply dtd_gain and echo_scale
                # dtd_gain controls aggressiveness, echo_scale corrects magnitude
                output = mic - self.dtd_gain * self.echo_scale * estimated_echo

            # If no leaky DTD, return early (fully freeze filter)
            if self.dtd_leak <= 0.0:
                output_int16 = np.clip(output * 32768.0, -32768, 32767).astype(np.int16)
                return output_int16

            # Leaky DTD: continue to filter update with reduced step size
            # This allows slow adaptation during speech while still protecting
            # the filter from being overwhelmed by speech energy
            mu_effective = self.mu * self.dtd_leak
        else:
            # Not in DTD - use full step size
            mu_effective = self.mu

        # === STEP 7: FDAF filter update ===
        # When in DTD with dtd_leak > 0, use reduced step size

        # Update power spectrum estimate (smoothed)
        X_power = np.abs(X) ** 2
        self._power_spectrum = self._power_alpha * X_power + (1 - self._power_alpha) * self._power_spectrum

        # Zero-pad error to FFT size for frequency-domain update
        # Use filter_error (unscaled) so the adaptive filter converges correctly
        e_padded = np.zeros(N, dtype=np.float64)
        e_padded[-L:] = filter_error  # Unscaled error for proper convergence

        # Error spectrum
        E = np.fft.fft(e_padded)

        # Normalized FDAF update: W += mu_effective * conj(X) * E / P
        # P is smoothed power spectrum + regularization
        # mu_effective is reduced during DTD if dtd_leak > 0
        P = self._power_spectrum + self._eps
        gradient = np.conj(X) * E / P
        self._W += mu_effective * gradient

        # === STEP 8: Causality constraint ===
        # The filter should be causal (only first filter_length coefficients)
        # Convert to time domain, zero out acausal part, convert back
        w_time = np.fft.ifft(self._W).real
        w_time[self.filter_length :] = 0  # Zero out acausal coefficients
        self._W = np.fft.fft(w_time)

        # Constrain filter energy to prevent divergence
        w_energy = np.sum(np.abs(self._W) ** 2)
        max_energy = 10.0 * self.fft_size  # Reasonable bound
        if w_energy > max_energy:
            self._W *= np.sqrt(max_energy / w_energy)

        # For fdaf_irm with mask_all_frames, apply IRM to ALL frames (not just DTD)
        # The filter update above used time-domain error for accurate adaptation.
        # Now replace output with IRM-processed version for better spectral masking.
        if self.dtd_mode == "fdaf_irm" and self.mask_all_frames:
            output = self._process_fdaf_irm(mic, self.echo_scale * estimated_echo)

        # Convert output to int16
        output_int16 = np.clip(output * 32768.0, -32768, 32767).astype(np.int16)
        return output_int16

    def reset(self) -> None:
        """Reset the AEC state (filter and buffers)."""
        self._W.fill(0)
        self._ref_buffer.fill(0)
        self._power_spectrum.fill(1e-6)
        self._baseline_error_power = 1e-6
        self._dtd_holdoff_counter = 0
        self._continuous_dtd_counter = 0
        self._dtd_force_release_remaining = 0
        self._frame_count = 0
        self._dtd_frame_count = 0
        self._erle_avg = 0.0
        self._erle_instant = 0.0
        self._mic_rms = 0.0
        self._output_rms = 0.0
        self._ref_rms = 0.0
        self._dtd_window.clear()
        self._ref_window.clear()
        self._erle_history.clear()
        self._last_error_to_baseline = 0.0
        self._silent_ref_bypass_count = 0
        self._silent_ref_bypass_active = False

        # Reset IRM-specific buffers if they exist
        if hasattr(self, "_mic_stft_buf"):
            self._mic_stft_buf.fill(0)
            self._echo_stft_buf.fill(0)

    def restart_warmup(self) -> None:
        """Restart warmup period without clearing the adaptive filter.

        Call when acoustic conditions change (e.g., playback starts).
        Keeps filter weights but resets DTD baseline so the filter can
        adapt to new conditions without DTD freezing it.
        """
        self._frame_count = 0
        self._baseline_error_power = 1e-6
        self._dtd_holdoff_counter = 0
        self._continuous_dtd_counter = 0
        self._dtd_force_release_remaining = 0
        self._silent_ref_bypass_active = False

    @property
    def dtd_frame_ratio(self) -> float:
        """Ratio of frames where DTD was active (frozen)."""
        if self._frame_count == 0:
            return 0.0
        return self._dtd_frame_count / self._frame_count

    def get_stats(self) -> dict:
        """Get AEC statistics for debugging."""
        # Compute filter norm in time domain
        w_time = np.fft.ifft(self._W).real
        filter_norm = np.linalg.norm(w_time[: self.filter_length])

        stats = {
            "frame_count": self._frame_count,
            "dtd_frame_count": self._dtd_frame_count,
            "dtd_frame_ratio": self.dtd_frame_ratio,
            "filter_norm": float(filter_norm),
            "filter_energy": float(np.sum(np.abs(self._W) ** 2)),
            "baseline_error_power": float(self._baseline_error_power),
            "dtd_mode": self.dtd_mode,
            "spectral_alpha": self.spectral_alpha,
            "dtd_gain": self.dtd_gain,
            "echo_scale": self.echo_scale,
            "silent_ref_bypass_frames": self._silent_ref_bypass_count,
            "silent_ref_bypass_active": self._silent_ref_bypass_active,
        }

        # Add IRM-specific stats
        if self.dtd_mode == "fdaf_irm":
            stats["mask_floor"] = self.mask_floor
            stats["mask_all_frames"] = self.mask_all_frames

        return stats

    def get_diagnostics(self) -> dict:
        """Get comprehensive AEC diagnostics for heartbeat, API, and AI tuning.

        Returns a dict with three groups:
        - Signal levels and ERLE (updated every frame)
        - Filter parameters (static config, but needed for tuning context)
        - Dynamic state (DTD, warmup, baseline, rolling stats)
        """
        dtd_ratio = sum(self._dtd_window) / len(self._dtd_window) if self._dtd_window else 0.0
        ref_activity = sum(self._ref_window) / len(self._ref_window) if self._ref_window else 0.0
        filter_energy = float(np.sum(np.abs(self._W) ** 2))

        # ERLE 60-second rolling stats
        erle_hist = self._erle_history
        if erle_hist:
            erle_arr = np.array(erle_hist)
            erle_60s_avg = float(np.mean(erle_arr))
            erle_60s_min = float(np.min(erle_arr))
            erle_60s_max = float(np.max(erle_arr))
        else:
            erle_60s_avg = erle_60s_min = erle_60s_max = 0.0

        return {
            # --- Signal levels (updated every frame) ---
            "erle_db": self._erle_avg,
            "erle_instant": self._erle_instant,
            "mic_rms": self._mic_rms,
            "output_rms": self._output_rms,
            "ref_rms": self._ref_rms,
            # --- Filter parameters (static config) ---
            "mu": self.mu,
            "filter_length_ms": int(self.filter_length * 1000 / self.sample_rate),
            "filter_length_taps": self.filter_length,
            "fft_size": self.fft_size,
            "echo_scale": self.echo_scale,
            "dtd_mode": self.dtd_mode,
            "dtd_gain": self.dtd_gain,
            "dtd_leak": self.dtd_leak,
            "dtd_threshold": self.dtd_threshold,
            "dtd_holdoff_ms": self.dtd_holdoff_frames * (self.frame_size * 1000 // self.sample_rate),
            "warmup_ms": self.warmup_frames * (self.frame_size * 1000 // self.sample_rate),
            "regularization_eps": self._eps,
            "power_smoothing_alpha": self._power_alpha,
            "baseline_smoothing_alpha": self._baseline_alpha,
            # --- Dynamic state ---
            "dtd_ratio": dtd_ratio,
            "dtd_holdoff_remaining": self._dtd_holdoff_counter,
            "warmup_remaining_frames": max(0, self.warmup_frames - self._frame_count),
            "baseline_error_power": float(self._baseline_error_power),
            "error_to_baseline_ratio": self._last_error_to_baseline,
            "filter_energy": filter_energy,
            "ref_active": ref_activity,
            "frames_processed": self._frame_count,
            "silent_ref_bypass_frames": self._silent_ref_bypass_count,
            "silent_ref_bypass_active": self._silent_ref_bypass_active,
            # --- 60-second rolling ERLE ---
            "erle_60s_avg": erle_60s_avg,
            "erle_60s_min": erle_60s_min,
            "erle_60s_max": erle_60s_max,
        }

    def set_dtd_mode(
        self,
        mode: str,
        alpha: float = 1.0,
        gain: float = 1.0,
        mask_floor: float = 0.0,
        mask_all_frames: bool = False,
    ) -> None:
        """
        Set the DTD output mode.

        Args:
            mode: "waveform" for standard, "spectral" for spectral subtraction,
                  "partial" for reduced cancellation, "fdaf_irm" for FDAF-estimated IRM
            alpha: Over-subtraction factor for spectral mode (1.0 = exact, >1.0 = aggressive)
            gain: Cancellation gain for partial mode (0.5 = half cancellation)
            mask_floor: Minimum mask value for fdaf_irm mode (0.0 = aggressive)
            mask_all_frames: For fdaf_irm, apply mask to all frames, not just DTD
        """
        valid_modes = ("waveform", "spectral", "partial", "fdaf_irm")
        if mode not in valid_modes:
            raise ValueError(f"Invalid dtd_mode: {mode}. Must be one of {valid_modes}")

        # Initialize IRM buffers if switching to fdaf_irm and not already initialized
        if mode == "fdaf_irm" and not hasattr(self, "_mic_stft_buf"):
            self._mic_stft_buf = np.zeros(self.STFT_WIN_LENGTH, dtype=np.float64)
            self._echo_stft_buf = np.zeros(self.STFT_WIN_LENGTH, dtype=np.float64)
            self._hann_window = get_window("hann", self.STFT_WIN_LENGTH, fftbins=False)
            self._irm_eps = 1e-10

        self.dtd_mode = mode
        self.spectral_alpha = alpha
        self.dtd_gain = gain
        self.mask_floor = mask_floor
        self.mask_all_frames = mask_all_frames
