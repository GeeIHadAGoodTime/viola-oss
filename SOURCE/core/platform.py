"""Centralized platform detection helpers for cross-platform support.

Provides consistent APIs for detecting the host platform, architecture,
audio subsystem, and XDG-compliant directory paths. All detection functions
are cached for single evaluation.

Used by config/settings.py, utils/enhancements/secrets.py, and install scripts
to ensure Viola works correctly on Windows, Linux desktop, and Raspberry Pi.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import tempfile
from collections.abc import Callable
from enum import Enum
from functools import lru_cache, wraps
from pathlib import Path, PurePosixPath

_logger = logging.getLogger(__name__)


class MalformedRuntimeDirError(RuntimeError):
    """A runtime-directory env override holds a value malformed for this platform.

    Raised when an explicit ``VIOLA_*_DIR`` override (data/cache/log/temp) is not
    absolute for the running platform — most commonly an MSYS/Git-Bash-mangled
    POSIX path on a Linux container (``/app/data`` rewritten into
    ``J:/Git/app/data``). Left unchecked, such a value silently ``resolve()``s
    into garbage under the current working directory (e.g. ``/app/J:/Git/app/data``
    on a read-only rootfs) and only surfaces much later as an opaque
    ``OSError: Read-only file system`` deep inside auth/bootstrap ``mkdir``.
    Failing here turns that into a loud, actionable startup error at the
    resolution site.
    """


class Platform(str, Enum):
    """Host operating system."""

    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


class AudioSubsystem(str, Enum):
    """Detected audio subsystem."""

    WASAPI = "wasapi"
    PIPEWIRE = "pipewire"
    PULSEAUDIO = "pulseaudio"
    ALSA = "alsa"
    COREAUDIO = "coreaudio"
    UNKNOWN = "unknown"


@lru_cache(maxsize=1)
def get_platform() -> Platform:
    """Detect the host operating system."""
    if sys.platform.startswith("win"):
        return Platform.WINDOWS
    if sys.platform.startswith("darwin"):
        return Platform.MACOS
    return Platform.LINUX


@lru_cache(maxsize=1)
def platform_download_key() -> str:
    """Return the update-manifest download key for the running install.

    This is the single vocabulary the desktop update client
    (``utils/update_checker.py``) and the version API
    (``updates/manifest.py``, ``admin/routes.py``) share for selecting a
    per-platform artifact out of a manifest's ``downloads`` block: one of
    ``windows_x64``, ``macos_arm64``, ``macos_x64``, ``linux_x86_64``.
    Windows is not architecture-split (no ARM64 Windows build exists), so
    every Windows install reports ``windows_x64`` regardless of CPU.
    """
    plat = get_platform()
    if plat == Platform.MACOS:
        return "macos_arm64" if is_arm() else "macos_x64"
    if plat == Platform.LINUX:
        return "linux_x86_64"
    return "windows_x64"


@lru_cache(maxsize=1)
def is_arm() -> bool:
    """Check if running on an ARM architecture (aarch64, armv7l, arm64)."""
    machine = platform.machine().lower()
    return machine in ("aarch64", "armv7l", "armv6l", "arm64")


@lru_cache(maxsize=1)
def is_raspberry_pi() -> bool:
    """Detect if running on a Raspberry Pi.

    Checks /proc/device-tree/model (most reliable) then falls back to
    /proc/cpuinfo BCM + ARM heuristic.
    """
    if sys.platform != "linux":
        return False

    try:
        model = Path("/proc/device-tree/model").read_text(encoding="utf-8").lower()
        if "raspberry" in model:
            return True
    except Exception:
        _logger.debug("Could not read /proc/device-tree/model for Pi detection")

    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8").lower()
        if "bcm" in cpuinfo and "arm" in cpuinfo:
            return True
    except Exception:
        _logger.debug("Could not read /proc/cpuinfo for Pi detection")

    return False


@lru_cache(maxsize=1)
def is_wayland() -> bool:
    """Check if the display server is Wayland."""
    return bool(os.environ.get("WAYLAND_DISPLAY"))


@lru_cache(maxsize=1)
def detect_audio_subsystem() -> AudioSubsystem:
    """Detect the host audio subsystem."""
    plat = get_platform()

    if plat == Platform.WINDOWS:
        return AudioSubsystem.WASAPI

    if plat == Platform.MACOS:
        return AudioSubsystem.COREAUDIO

    # Linux: check for PipeWire first, then PulseAudio, then ALSA
    # PipeWire often runs a PulseAudio compatibility layer, so check it first
    try:
        import shutil

        if shutil.which("pw-cli") is not None:
            return AudioSubsystem.PIPEWIRE
    except Exception:
        _logger.debug("PipeWire detection via shutil.which failed")

    if os.environ.get("PULSE_SERVER") or os.path.exists(os.path.expanduser("~/.config/pulse")):
        return AudioSubsystem.PULSEAUDIO

    # Check for PulseAudio socket
    pulse_runtime = os.environ.get(
        "PULSE_RUNTIME_PATH",
        f"/run/user/{os.getuid()}/pulse" if hasattr(os, "getuid") else "",
    )
    if pulse_runtime and os.path.exists(pulse_runtime):
        return AudioSubsystem.PULSEAUDIO

    # ALSA fallback
    if os.path.exists("/proc/asound"):
        return AudioSubsystem.ALSA

    return AudioSubsystem.UNKNOWN


# ---------------------------------------------------------------------------
# XDG-compliant directory helpers
# ---------------------------------------------------------------------------

_APP_NAME = "viola"
_PROJECT_ROOT_ENV = "VIOLA_PROJECT_ROOT"
_USE_SYSTEM_TEMP_ENV = "VIOLA_USE_SYSTEM_TEMP"
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on", "y"})

# Every env var any runtime-directory resolver below reads, directly or through a
# sibling resolver it delegates to. This tuple IS the memo key for all of them (see
# ``_env_scoped_cache``), so adding an env read to one of those functions means adding
# the var here too — otherwise that resolver goes back to answering from a stale
# snapshot the moment the value changes.
_RUNTIME_DIR_ENV_VARS = (
    _PROJECT_ROOT_ENV,
    "VIOLA_DATA_DIR",
    "VIOLA_CACHE_DIR",
    "VIOLA_LOG_DIR",
    "VIOLA_TEMP_DIR",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "LOCALAPPDATA",
)

# Distinct env snapshots kept warm. Small on purpose: a process normally sees one or
# two, while a test session legitimately sees one per temp dir, and evicting the
# oldest just means recomputing — never returning a wrong directory.
_RUNTIME_DIR_CACHE_SLOTS = 16


def _runtime_dir_env_key() -> tuple[str | None, ...]:
    """Snapshot every env var the runtime-directory resolvers depend on."""
    return tuple(os.environ.get(name) for name in _RUNTIME_DIR_ENV_VARS)


def _env_scoped_cache(func: Callable[[], Path]) -> Callable[[], Path]:
    """Memoize a runtime-directory resolver PER runtime-dir env snapshot.

    These resolvers used to be ``@lru_cache(maxsize=1)``, which memoized on "no
    arguments" and therefore froze each one on its very first call for the whole life
    of the process — even though every one of them documents an explicit
    ``VIOLA_*_DIR`` / ``XDG_*`` env override as its highest-priority input. The first
    call happens at IMPORT time, far earlier than anything intends: importing
    ``config`` builds the module-level ``AppConfig`` singleton (``config/settings.py``
    ``settings: AppConfig = get_settings()``), whose ``data_dir`` default calls
    ``get_data_dir()``. From that point on, setting or changing ``VIOLA_DATA_DIR`` was
    silently a no-op and the process kept writing to the pre-import directory.

    Incident (2026-07-29, run 30499729474, safety-core P0): every test that isolates
    itself with ``monkeypatch.setenv("VIOLA_DATA_DIR", str(tmp_path))`` was getting no
    isolation at all, so ``tests/unit/services/payments/test_payment_vault_cvc_roundtrip.py``
    wrote a real Tier-3 encrypted payment vault into the REAL user data dir
    (``/home/runner/.local/share/viola/payment_vault_payment-owner.enc``) instead of its
    temp dir. That file outlived the run, and the next run's freshly generated vault key
    could not decrypt it, so ``save_card`` raised ``RuntimeError: payment vault could not
    be decrypted`` and the ``sensitive-decrypt-access-paths`` safety gate went red on a
    machine-state artifact rather than on any isolation defect. It reddened every run
    scheduled onto an already-polluted runner while passing on fresh ones, which is what
    made it look like flake. Same bug class as the 2026-07-25 fix in
    ``services/payments/payment_vault.py``: Tier-3 cardholder data landing in a directory
    nobody intended.

    Keying the memo on the env snapshot keeps the caching (resolution + validation still
    runs once per distinct environment) while making a changed override take effect, which
    is what every one of these docstrings already promises. ``cache_clear`` /
    ``cache_info`` stay exposed on the public name so existing callers keep working.
    """
    cached = lru_cache(maxsize=_RUNTIME_DIR_CACHE_SLOTS)(lambda _env_key: func())

    @wraps(func)
    def wrapper() -> Path:
        return cached(_runtime_dir_env_key())

    wrapper.cache_clear = cached.cache_clear  # type: ignore[attr-defined]
    wrapper.cache_info = cached.cache_info  # type: ignore[attr-defined]
    return wrapper


@_env_scoped_cache
def get_project_root() -> Path:
    """Return the repository/application root used for local runtime state."""

    configured = os.environ.get(_PROJECT_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_VALUES


def _is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def _resolve_explicit_runtime_dir(env_name: str, raw: str) -> Path:
    """Validate + resolve an explicit ``VIOLA_*_DIR`` override, or fail loudly.

    A runtime-directory override that a deploy passes via env MUST be absolute for
    the platform actually running the process; a relative value silently
    ``resolve()``s against the current working directory and produces garbage.
    The concrete production incident (Sentry 7592746092): a Linux container got
    ``VIOLA_DATA_DIR=J:/Git/app/data`` because MSYS/Git-Bash rewrote the intended
    POSIX ``/app/data`` into ``<git-bash-root>/app/data`` before Docker saw it.
    On Linux that string is RELATIVE, so ``get_data_dir()`` resolved it under the
    read-only ``/app`` as ``/app/J:/Git/app/data`` and ``mkdir`` blew up much later
    inside auth bootstrap with an opaque read-only-filesystem error.

    Scope — POSIX only, deliberately. The read-only-rootfs disaster is a
    container/POSIX condition: a relative POSIX override resolves under ``/app``
    (read-only) and detonates. Absoluteness is therefore enforced only when
    ``get_platform()`` is not Windows. On Windows a relative override
    (``VIOLA_DATA_DIR=./.viola`` is a documented dev pattern — see
    ``.env.example``) resolves predictably under the writable cwd and stays
    permitted, so Windows desktop dev behavior is byte-for-byte unchanged. The
    check runs against ``get_platform()`` + ``PurePosixPath`` (not the ambient
    ``Path`` class) so it is both correct in the real Linux container AND testable
    from a Windows CI host: ``J:/Git/app/data`` (no leading ``/``) is rejected
    while the canonical ``/app/data`` passes.
    """
    expanded = os.path.expanduser(raw)

    if get_platform() != Platform.WINDOWS and not PurePosixPath(expanded).is_absolute():
        plat = get_platform().value
        raise MalformedRuntimeDirError(
            f"{env_name} is set to a non-absolute path {raw!r}, which is invalid on "
            f"this platform ({plat}). A runtime-directory override must be an absolute "
            f"path (POSIX example: '/app/data'). A non-absolute value would silently "
            f"resolve against the current working directory and create a garbage path "
            f"under a possibly read-only root, failing later with an opaque mkdir error. "
            f"This usually means the value was mangled by MSYS/Git-Bash path rewriting "
            f"when the container/process was launched from a Git Bash shell (it rewrote "
            f"POSIX '/app/data' into the Git-Bash install root + '/app/data', e.g. "
            f"'J:/Git/app/data'). Fix: set {env_name} to an absolute path, and launch "
            f"the container from PowerShell/cmd or disable MSYS path conversion "
            f"(MSYS_NO_PATHCONV=1 / MSYS2_ARG_CONV_EXCL='*') so the value is not rewritten."
        )
    return Path(expanded).resolve()


def _frozen_windows_data_root() -> Path:
    """Per-user writable data root for an installed Windows build.

    A frozen install's ``get_project_root()`` points at the install directory
    (e.g. ``C:\\Program Files\\Viola\\_internal``), which is NOT writable for a
    normal user. Writing runtime state there throws ``PermissionError`` at the
    very first startup call (``configure_environment``), and because the app is
    built ``console=False`` the process dies with no window and no error — the
    "app does not start on a clean machine" bug. Writable runtime state for an
    installed build therefore lives under ``%LOCALAPPDATA%\\Viola`` (honoring
    ``LOCALAPPDATA`` so redirected/off-system-drive profiles still work), never
    the install directory. ``get_project_root()`` is left untouched so bundled
    read-only resources keep resolving against the install tree.
    """
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Viola"


@_env_scoped_cache
def get_data_dir() -> Path:
    """Return the platform-appropriate data directory.

    Priority: explicit ``VIOLA_DATA_DIR`` env (deploy / container override)
    -> Windows project default -> ``XDG_DATA_HOME/viola`` -> ``~/.local/share/viola``.

    The explicit env-var path is required for read-only-rootfs containers
    (cloud Docker on Fly / Cloudflare-tunnelled self-host) where
    ``Path.home()`` resolves to a non-writable directory but the deploy
    mounts a writable volume at a known location. ``configure_environment``
    already writes ``VIOLA_DATA_DIR`` for subprocesses; honoring it here
    keeps the import-time vs subprocess views consistent.
    """
    explicit = os.environ.get("VIOLA_DATA_DIR")
    if explicit:
        return _resolve_explicit_runtime_dir("VIOLA_DATA_DIR", explicit)

    if get_platform() == Platform.WINDOWS:
        # Installed (frozen) builds must NOT write under the install dir — see
        # _frozen_windows_data_root. Dev runs keep state in the repo's .viola.
        if _is_frozen():
            return _frozen_windows_data_root()
        return get_project_root() / ".viola"

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path.home() / ".local" / "share" / _APP_NAME


@_env_scoped_cache
def get_config_dir() -> Path:
    """Return the platform-appropriate config directory.

    - Windows: <project>/.viola
    - Linux/macOS: $XDG_CONFIG_HOME/viola or ~/.config/viola
    """
    if get_platform() == Platform.WINDOWS:
        return get_data_dir()

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path.home() / ".config" / _APP_NAME


@_env_scoped_cache
def get_cache_dir() -> Path:
    """Return the platform-appropriate cache directory.

    Priority: explicit ``VIOLA_CACHE_DIR`` env (deploy / container override)
    -> Windows ``<project>/.viola/cache`` -> ``XDG_CACHE_HOME/viola`` ->
    ``~/.cache/viola``.

    Same rationale as ``get_data_dir`` for the env-var-first ordering —
    cloud Docker / Fly mount a writable cache volume and pass the path via
    env, but the original logic ignored it and fell through to ``~/.cache``
    which sits on the read-only rootfs.
    """
    explicit = os.environ.get("VIOLA_CACHE_DIR")
    if explicit:
        return _resolve_explicit_runtime_dir("VIOLA_CACHE_DIR", explicit)

    if get_platform() == Platform.WINDOWS:
        return get_data_dir() / "cache"

    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path.home() / ".cache" / _APP_NAME


@_env_scoped_cache
def get_logs_dir() -> Path:
    """Return the project-owned runtime log directory.

    Priority: ``VIOLA_LOG_DIR`` env -> ``<data_dir>/logs`` when an explicit
    ``VIOLA_DATA_DIR`` was provided (so deploys with a writable data volume
    get matching writable logs) -> ``<project>/logs`` for local dev.

    The cascade keeps desktop dev behavior unchanged (logs land in
    ``<project>/logs``) while ensuring read-only-rootfs containers don't
    fall through to ``/app/logs`` (which is on the read-only base layer).
    """
    configured = os.environ.get("VIOLA_LOG_DIR")
    if configured:
        return _resolve_explicit_runtime_dir("VIOLA_LOG_DIR", configured)
    if os.environ.get("VIOLA_DATA_DIR"):
        return get_data_dir() / "logs"
    # Frozen installs: logs must follow the writable data dir, not the
    # (read-only) install directory get_project_root() resolves to.
    if _is_frozen():
        return get_data_dir() / "logs"
    return get_project_root() / "logs"


@_env_scoped_cache
def get_temp_dir() -> Path:
    """Return Viola's temp directory for transient runtime files.

    Honors ``VIOLA_TEMP_DIR`` env (deploy override) before falling through
    to ``<data_dir>/tmp``. Same env-var-first ordering as ``get_data_dir``
    / ``get_cache_dir`` — read-only-rootfs containers need this redirected
    to a writable tmpfs or volume.
    """
    explicit = os.environ.get("VIOLA_TEMP_DIR")
    if explicit:
        return _resolve_explicit_runtime_dir("VIOLA_TEMP_DIR", explicit)
    return get_data_dir() / "tmp"


def _set_env_path(name: str, path: Path, *, override: bool = False) -> None:
    if override or not os.environ.get(name):
        os.environ[name] = str(path)


def configure_environment() -> dict[str, str]:
    """Pin third-party cache/temp locations to Viola-controlled directories.

    On Windows, many libraries default to `%USERPROFILE%`, `%LOCALAPPDATA%`,
    or `%TEMP%` on C:.  Call this before importing cache-heavy libraries or
    launching subprocesses so inherited environment defaults stay under the
    project-owned `.viola` tree.
    """

    data_dir = get_data_dir().resolve()
    cache_dir = get_cache_dir().resolve()
    temp_dir = get_temp_dir().resolve()

    env_paths = {
        "VIOLA_DATA_DIR": data_dir,
        "VIOLA_CACHE_DIR": cache_dir,
        "VIOLA_TEMP_DIR": temp_dir,
        "VIOLA_LOG_DIR": get_logs_dir().resolve(),
        "XDG_DATA_HOME": data_dir / "xdg-data",
        "XDG_CONFIG_HOME": data_dir / "xdg-config",
        "XDG_CACHE_HOME": cache_dir / "xdg-cache",
        "PIP_CACHE_DIR": cache_dir / "pip",
        "NPM_CONFIG_CACHE": cache_dir / "npm",
        "CARGO_HOME": data_dir / "cargo",
        "PLAYWRIGHT_BROWSERS_PATH": cache_dir / "playwright-browsers",
        "HF_HOME": cache_dir / "huggingface",
        "HF_HUB_CACHE": cache_dir / "huggingface" / "hub",
        "TRANSFORMERS_CACHE": cache_dir / "huggingface" / "transformers",
        "TORCH_HOME": cache_dir / "torch",
        "SENTENCE_TRANSFORMERS_HOME": cache_dir / "sentence-transformers",
        "NLTK_DATA": data_dir / "nltk_data",
        "WHISPER_CACHE_DIR": cache_dir / "whisper",
        "TIKTOKEN_CACHE_DIR": cache_dir / "tiktoken",
        "NUMBA_CACHE_DIR": cache_dir / "numba",
        "MPLCONFIGDIR": data_dir / "matplotlib",
    }
    env_values = {
        # codex-auth auto-patches OpenAI clients on bare import and its default
        # token store is under the user's home directory. Viola creates an
        # explicit transport with a project-local TokenStore instead.
        "CODEX_AUTH_NO_PATCH": "1",
    }

    for path in {data_dir, cache_dir, temp_dir, *env_paths.values()}:
        path.mkdir(parents=True, exist_ok=True)

    for name, path in env_paths.items():
        _set_env_path(name, path)
    for name, value in env_values.items():
        if not os.environ.get(name):
            os.environ[name] = value

    if get_platform() == Platform.WINDOWS and not _env_truthy(_USE_SYSTEM_TEMP_ENV):
        for name in ("TEMP", "TMP", "TMPDIR"):
            _set_env_path(name, temp_dir, override=True)
    else:
        _set_env_path("TMPDIR", temp_dir)

    # Authoritatively pin Python's stdlib tempfile module to the Viola-controlled
    # temp dir. Setting os.environ["TMPDIR"] above is NOT sufficient on its own:
    # ``tempfile`` computes and CACHES ``tempfile.tempdir`` on the first
    # ``gettempdir()``/``mkdtemp()`` call anywhere in the process, after which
    # every later TMPDIR env change is ignored. Cloud startup imports a large
    # dependency graph before the phone Kokoro TTS prewarm, so tempfile is almost
    # always already cached to the system ``/tmp`` by the time phonemizer runs.
    #
    # The hardened cloud container mounts ``/tmp`` as a NOEXEC tmpfs. phonemizer's
    # ``EspeakAPI`` (phonemizer/backend/espeak/api.py) copies ``libespeak-ng.so``
    # into ``tempfile.mkdtemp()`` and ``dlopen()``s the copy — on a noexec mount
    # that dlopen dies with ``OSError: .../libespeak-ng.so: failed to map segment
    # from shared object``, which hard-fails fail-closed cloud startup and takes
    # the ENTIRE cloud API down to protect the phone-TTS surface (#3461: 6x
    # 2026-07-04..18; roll-preflight incident 2026-07-18). The container also
    # exports TMPDIR (docker-compose.cloud.yml) as a belt, but that env export is
    # a single point of failure — a new service, smoke container, or worker that
    # forgets it reintroduces the crash. Assigning ``tempfile.tempdir`` here makes
    # the in-process guarantee hold regardless of import order OR whether TMPDIR
    # was exported, so the espeak copy always lands on the writable+exec volume.
    #
    # Honors the same VIOLA_USE_SYSTEM_TEMP escape hatch as the Windows branch: if
    # set, leave tempfile on its OS default and do not pin.
    if not _env_truthy(_USE_SYSTEM_TEMP_ENV):
        tempfile.tempdir = str(temp_dir)

    return {
        name: os.environ[name]
        for name in (*env_paths.keys(), *env_values.keys(), "TEMP", "TMP", "TMPDIR")
        if name in os.environ
    }


__all__ = [
    "AudioSubsystem",
    "MalformedRuntimeDirError",
    "Platform",
    "configure_environment",
    "detect_audio_subsystem",
    "get_cache_dir",
    "get_config_dir",
    "get_data_dir",
    "get_logs_dir",
    "get_platform",
    "get_project_root",
    "get_temp_dir",
    "is_arm",
    "is_raspberry_pi",
    "is_wayland",
    "platform_download_key",
]
