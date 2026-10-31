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

# ============================================================
# STREAMING (80 ms) SCORING -- wake plan Stage 1
# ============================================================
#
# The shipped app used to re-embed the whole 1.5 s buffer and score it once
# every 400 ms, and woke on ONE score at or above the threshold. "Viola" is
# about 0.6 s long, so at 400 ms the model saw the word once or twice, and any
# agreement rule cost half the real wakes. The engine now computes one
# openWakeWord embedding per 80 ms chunk incrementally (the same thing
# openWakeWord's own streaming path does), scores the temporal CNN on every
# chunk, and wakes only when STREAM_AGREE_K of the last STREAM_AGREE_N scores
# (the current one included) are at or above the threshold.
#
# The rule was chosen by measurement on never-seen speech (21.7 h of MUSAN
# us-gov + LibriVox) and on the 542 real Jay/Sierra recordings; see
# docs/wake/STAGE1_CADENCE_AGREEMENT.md in the private repo for the tables.
STREAM_CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz: one embedding per chunk
STREAM_AGREE_K = 7
STREAM_AGREE_N = 7

# The saved ``wake_sensitivity`` stays the user's sensitivity LEVEL (0.80 to
# 0.97, higher = stricter, 0.90 = Balanced; quiet hours add 0.08, capped at
# 0.98). On the streaming path each level is one measured operating point:
# (level, k, n, per-frame score threshold). Agreement over 7 frames is far
# stricter than one frame, so the per-frame threshold is lower than the level.
# Every row has fewer false wakes than the old single-frame 400 ms rule at the
# same level AND at least its recall on the real recordings (doc above).
STREAM_OPERATING_POINTS: tuple[tuple[float, int, int, float], ...] = (
    (0.80, 4, 5, 0.74),  # Most sensitive
    (0.84, 5, 6, 0.78),  # More sensitive
    (0.87, 5, 6, 0.81),  # Slightly more sensitive
    (0.90, 7, 7, 0.79),  # Balanced (default)
    (0.93, 7, 7, 0.83),  # Slightly stricter
    (0.95, 7, 7, 0.87),  # Fewer false wakes
    (0.97, 7, 7, 0.92),  # Fewest false wakes
    (0.98, 7, 7, 0.94),  # quiet hours at Balanced (0.90 + 0.08, capped)
)

# With the Stage 2 wake check on (the opt-in "Double-check the wake word"
# setting), Balanced uses 6 of 7 frames at 0.85 instead of 7 of 7 at 0.79. The
# check removes most speech false wakes either way; this rule lets far fewer
# through to it over music from another device (92 vs 141 in 42.6 h of MUSAN
# music), confirms about 65 ms sooner, and with the check keeps at least 90% of
# real wakes in every recorded set (build-5 wake-opt measurement note).
#
# A stricter level must stay stricter than that Balanced rule. "Slightly
# stricter" used to keep its unchecked 7 of 7 at 0.83, which the log showed as
# the frame threshold going DOWN from 0.85 (1.0.5 build-7 laptop report, A10.6),
# and which is not a subset of Balanced: seven frames at 0.84 woke it but not
# Balanced. With the check on, a stricter level now never uses a frame
# threshold below Balanced's 0.85 (so 7 of 7 at 0.85 there: every wake it
# allows, Balanced allows too). Wake-opt data (49.9 h speech, 42.6 h music):
# 7/7@0.85 gives 12 speech / 54 music candidates, 6/7@0.85 gives 22 / 92 and
# the old 7/7@0.83 gave 15 / 83. More sensitive levels and quiet hours are
# unchanged.
_CHECKED_BALANCED_RULE = (6, 7, 0.85)


def _checked_row(level: float, k: int, n: int, frame: float) -> tuple[float, int, int, float]:
    bk, _bn, bframe = _CHECKED_BALANCED_RULE
    if level == DEFAULT_THRESHOLD:
        return (level, *_CHECKED_BALANCED_RULE)
    if level > DEFAULT_THRESHOLD:
        return (level, max(k, bk), n, max(frame, bframe))
    return (level, k, n, frame)


STREAM_OPERATING_POINTS_CHECKED: tuple[tuple[float, int, int, float], ...] = tuple(
    _checked_row(level, k, n, frame) for level, k, n, frame in STREAM_OPERATING_POINTS
)

# Music path (1.0.5 build-7 laptop report, A0.6 and "wake vs music"): with
# vocal music playing at volume 80 the real "Viola" topped out at 0.815-0.896
# per frame, so 6 of 7 frames at 0.85 never agreed (instrumental music or
# silence: 0.95-0.995). While Viola's own music plays and the Stage 2 check is
# on, the engine also wakes on this looser rule, but such a wake passes Stage 2
# only when Whisper confidently hears a Viola-like word (``require_token``;
# doubt rejects). A candidate the normal rule also confirms within its own
# window takes the normal path. Measured on the wake-opt data: 321 music
# candidates in 42.6 h, 0 passed (6 decoded a bare "Viola," echo of the prompt,
# all with no_speech >= 0.6 and logprob <= -1.0, which the check does not
# trust); 103 room-speech candidates in 49.9 h, 2 passed ("Rubiola", "Fula").
# On real recordings over music it detects 95-99% and Whisper hears the token
# in about 62% of them, so a masked "Viola" now wakes about 6 times in 10
# instead of never. 4 of 5 at 0.74 was also measured and let 8 music
# candidates through, so it was not chosen. Only at Balanced or more sensitive
# levels and outside quiet hours (stricter choices keep their own rule).
MUSIC_WAKE_RULE: tuple[int, int, float] = (5, 6, 0.78)


def stream_operating_point(level: float, *, checked: bool = False) -> tuple[int, int, float]:
    """Return (k, n, frame_threshold) for a sensitivity level.

    Uses the nearest measured level; an exact tie goes to the stricter row, the
    same rule the Settings slider uses to show a saved value as a position.
    ``checked``: the Stage 2 wake check is on (STREAM_OPERATING_POINTS_CHECKED).
    """
    try:
        lv = float(level)
    except (TypeError, ValueError):
        lv = DEFAULT_THRESHOLD
    if lv != lv:  # NaN
        lv = DEFAULT_THRESHOLD
    table = STREAM_OPERATING_POINTS_CHECKED if checked else STREAM_OPERATING_POINTS
    best = min(table, key=lambda row: (round(abs(row[0] - lv), 9), -row[0]))
    return best[1], best[2], best[3]


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
