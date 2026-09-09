"""
ViolaWake Configuration
========================

Constants for audio processing and model configuration.
These MUST match the values used during training.
"""

from __future__ import annotations

from pathlib import Path

from config.constants import AUDIO_SAMPLE_RATE

# ============================================================
# AUDIO SETTINGS
# ============================================================

# Sample rate (must match training data)
SAMPLE_RATE = AUDIO_SAMPLE_RATE

# Clip duration in seconds
CLIP_DURATION = 1.5

# Number of samples per clip
CLIP_SAMPLES = int(SAMPLE_RATE * CLIP_DURATION)

# ============================================================
# FEATURE EXTRACTION SETTINGS
# ============================================================

# Feature type selector: "linear" (legacy v2 model) or "mel" / "mel_pcen" (v3+)
# "linear" = scipy spectrogram, first 32 FFT bins (~0-1280 Hz)
# "mel" = librosa mel spectrogram with log compression (60-7800 Hz)
# "mel_pcen" = librosa mel spectrogram with PCEN compression (60-7800 Hz)
# MUST be "mel_pcen" to match v3 model training (pipeline audit 2026-03-02).
# Using "mel" with a PCEN-trained model produces inverted scores (0.003 on
# positives, 1.0 on negatives) — confirmed live production bug 2026-03-02.
FEATURE_TYPE = "mel_pcen"

# ============================================================
# LINEAR SPECTROGRAM SETTINGS (legacy, used by viola_v2.onnx)
# ============================================================

# Number of linear frequency bins (legacy — first N bins of scipy FFT)
N_MELS = 32

# FFT window size
N_FFT = 512

# Hop length (10ms at 16kHz)
HOP_LENGTH = 160

# Window length (25ms at 16kHz)
WIN_LENGTH = 400

# ============================================================
# MEL SPECTROGRAM V2 SETTINGS (for retraining with full speech coverage)
# ============================================================

# Number of mel frequency bins — 40 captures F1/F2/F3 formants
N_MELS_MEL = 40

# FFT window size for mel spectrogram
N_FFT_MEL = 512

# Hop length for mel spectrogram (10ms at 16kHz)
HOP_LENGTH_MEL = 160

# Window length for mel spectrogram (25ms at 16kHz)
WIN_LENGTH_MEL = 400

# Minimum frequency for mel filterbank (Hz) — captures fundamental F0
F_MIN = 60

# Maximum frequency for mel filterbank (Hz) — covers F3 + sibilants
F_MAX = 7800

# ============================================================
# PCEN (Per-Channel Energy Normalization) SETTINGS
# ============================================================
# PCEN replaces log compression with an adaptive normalization that is
# more robust to varying volume levels and background noise. Formula:
#   y = (mel / (eps + smoother))^gain + bias)^power - bias^power
# Reference: Wang et al. 2017, "Trainable Frontend for Robust and
# Far-Field Keyword Spotting"

# Whether to use PCEN instead of log compression for mel features.
# MUST be True when using v3 model (trained with PCEN). Setting False
# with a PCEN-trained model is a live bug — see pipeline audit 2026-03-02.
USE_PCEN = True

# PCEN gain (alpha) — controls AGC strength; higher = more normalization
PCEN_GAIN = 0.98

# PCEN bias (delta) — stabilizes the ratio for near-silent frames
PCEN_BIAS = 2.0

# PCEN power (r) — root compression exponent; 0.5 = sqrt-like compression
PCEN_POWER = 0.5

# PCEN time constant (seconds) — IIR smoothing filter time constant
# At 16kHz/hop=160: ~100 frames/s, 0.06s = ~6 frame averaging window
PCEN_TIME_CONSTANT = 0.06

# PCEN epsilon — numerical stability floor
PCEN_EPS = 1e-6

# ============================================================
# MODEL SETTINGS
# ============================================================

# Hidden layer size in classifier
HIDDEN_SIZE = 64

# Default detection threshold
# Raised from 0.50 to 0.80 for temporal_cnn model (2026-03-27), then aligned
# to the launch wake_sensitivity default 0.90 after the 2026-06-05 battery.
DEFAULT_THRESHOLD = 0.90

# ============================================================
# PATHS
# ============================================================

# Project root (computed dynamically). In a frozen (installed) build this is
# the read-only install tree (_internal) — bundled model READS resolve here.
PROJECT_ROOT = Path(__file__).parent.parent

# Bundled data root — READ-ONLY at runtime (PATH-1). Holds the trained wake
# models shipped with the app. Runtime WRITES must never land here: in a
# frozen install this is the install dir (PermissionError / install-dir
# pollution), and as a cwd-relative path it breaks under arbitrary launch
# cwds. All mutable state goes through wake_runtime_data_dir() instead.
DATA_DIR = PROJECT_ROOT / "violawake_data"

# Model output directory (read-only bundled models at runtime; training
# scripts write here on dev machines only).
MODELS_DIR = DATA_DIR / "trained_models"

# Bundled OpenWakeWord preprocessor payloads (melspectrogram + embedding).
# Tracked in git at models/wake/ and collected into the frozen bundle by the
# spec's ("models", "models") datas entry — the wake detector must NEVER
# download these at runtime (requal-M3: fresh installs need no network and
# no install-dir writes).
OWW_PREPROCESSOR_DIR = PROJECT_ROOT / "models" / "wake"


def wake_runtime_data_dir() -> Path:
    """Writable root for ViolaWake mutable runtime state (PATH-1 canon).

    All runtime writes (false positives, contributor samples, calibration
    profiles) anchor on ``core.platform.get_data_dir()`` — never on the
    project root, the install dir, or the process cwd. Resolved lazily so
    ``VIOLA_DATA_DIR`` / ``configure_environment`` ordering is honored.
    """
    from core.platform import get_data_dir

    return get_data_dir() / "violawake"


# Default model path — temporal CNN wake word model.
# Previous defaults: viola_mlp_oww.onnx (MLP, d-prime 15.10),
# viola_mlp_oww_maxpool.onnx (d-prime 3.07, music FP problem).
DEFAULT_MODEL_PATH = MODELS_DIR / "temporal_cnn.onnx"

# ============================================================
# INFERENCE SETTINGS
# ============================================================

# Debounce window in seconds (prevent multiple triggers)
# Matches test_violawake_model.py default for consistent behavior
DEBOUNCE_SECONDS = 2.0

# RMS silence gate threshold (float32 scale).
# Audio with RMS below this is skipped before inference.
# Lowered from 0.005 to 0.001 (2026-03-06): 0.005 rejected 29/113
# positive eval files (legitimate whisper/quiet speech), dropping
# d-prime from 1.45 to 0.55 and recall from 95% to 72%.
# NOTE: This threshold was tuned for 16 kHz input. Callers must feed
# audio at ``SAMPLE_RATE`` — the engine enforces this at process_audio
# entry and raises if violated (see violawake/engine.py).
SILENCE_GATE_RMS = 0.001

# Minimum score to log as potential detection
LOG_THRESHOLD = 0.3


def get_feature_config() -> dict:
    """Return the complete feature configuration dict.

    This dict is saved inside model checkpoints at training time
    and verified at inference time. If the checkpoint's feature
    config doesn't match the current config, inference will use
    the CHECKPOINT's config (not config.py) and log an error.

    Added 2026-03-02 after config drift bug caused inverted
    scores in production.
    """
    return {
        "feature_type": FEATURE_TYPE,
        "n_mels": N_MELS_MEL,
        "n_fft": N_FFT_MEL,
        "hop_length": HOP_LENGTH_MEL,
        "win_length": WIN_LENGTH_MEL,
        "f_min": F_MIN,
        "f_max": F_MAX,
        "sample_rate": SAMPLE_RATE,
        "clip_samples": CLIP_SAMPLES,
        "use_pcen": USE_PCEN,
        "pcen_gain": PCEN_GAIN,
        "pcen_bias": PCEN_BIAS,
        "pcen_power": PCEN_POWER,
        "pcen_time_constant": PCEN_TIME_CONSTANT,
        "pcen_eps": PCEN_EPS,
    }
