"""Public release manifest loading and backward-compatible projection."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.constants import VIOLA_VERSION
from updates.rollout import (
    COHORT_ALGORITHM,
    DEFAULT_ROLLOUT_STEPS,
    normalize_rollout_percent,
    parse_rollout_steps,
)

VALID_CHANNELS = frozenset({"stable", "beta"})
DEFAULT_RELEASED = "2026-02-25"
DEFAULT_MANIFEST_DIR = Path("updates") / "manifests"

# The vocabulary shared with core.platform.platform_download_key() and the
# desktop client (utils/update_checker.py). "windows_x64" is the reference
# platform: it is the only key ever selected implicitly (platform=None), so
# Windows clients see byte-identical behavior whether or not this feature
# exists. The others select a real per-platform artifact when one has been
# published into a manifest's `downloads` block, and otherwise fall back to
# the generic download page rather than serving the wrong platform's binary.
KNOWN_PLATFORM_KEYS = frozenset({"windows_x64", "macos_arm64", "macos_x64", "linux_x86_64"})
REFERENCE_PLATFORM_KEY = "windows_x64"
GENERIC_DOWNLOAD_PAGE_URL = "https://useviola.com/download"


def normalize_channel(channel: str | None) -> str:
    normalized = str(channel or "stable").strip().lower()
    return normalized if normalized in VALID_CHANNELS else "stable"


def normalize_platform_key(platform: str | None) -> str | None:
    """Return `platform` if it is a recognized download key, else None.

    None means "no platform signal" — build_public_manifest keeps its
    original Windows-reference behavior. Callers (the version API route,
    the desktop client) should pass raw/untrusted input straight through
    this before using it, so an unrecognized or absent value can never
    select an unexpected `downloads` entry.
    """
    normalized = str(platform or "").strip().lower()
    return normalized if normalized in KNOWN_PLATFORM_KEYS else None


def configured_manifest_dir() -> Path:
    try:
        from config.settings import settings
    except ImportError:
        return DEFAULT_MANIFEST_DIR

    configured = str(getattr(settings, "release_manifest_dir", "") or "").strip()
    return Path(configured) if configured else DEFAULT_MANIFEST_DIR


def manifest_path(channel: str = "stable", manifest_dir: str | Path | None = None) -> Path:
    base = Path(manifest_dir) if manifest_dir is not None else configured_manifest_dir()
    return base / f"{normalize_channel(channel)}.json"


def load_release_manifest(channel: str = "stable", manifest_dir: str | Path | None = None) -> dict[str, Any]:
    normalized_channel = normalize_channel(channel)
    path = manifest_path(normalized_channel, manifest_dir)
    if not path.exists():
        return _default_manifest(normalized_channel)

    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        return _default_manifest(normalized_channel)
    loaded["channel"] = normalized_channel
    return loaded


def build_public_manifest(
    channel: str = "stable",
    manifest_dir: str | Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Return the additive public manifest served to update clients.

    The legacy top-level version fields are projected conservatively for the
    stable channel: partial or frozen rollouts expose the previous stable
    version to old clients, while rollout-aware clients read the candidate and
    rollout fields.

    ``platform`` is an optional download key (see ``KNOWN_PLATFORM_KEYS``,
    e.g. ``"macos_arm64"``). When given and recognized:

    - ``"windows_x64"`` (or an unrecognized/omitted value) leaves every field
      exactly as computed above — this is the reference platform and its
      response must never change shape because of this parameter.
    - Any other known key looks up ``downloads[platform]`` in the *already
      loaded* manifest. If that entry has a URL, the top-level ``url``,
      ``installer_url``, ``sha256``/``installer_sha256`` and
      ``candidate.url``/``candidate.sha256`` are overridden with it, and
      ``platform_artifact_available`` is set True — the mac client gets a mac
      artifact once one is published.
    - If no such entry exists yet (no artifact published for that platform),
      ``url``/``installer_url`` fall back to the generic download page
      instead of silently serving another platform's installer, and
      ``platform_artifact_available`` is set False.
    """
    manifest = copy.deepcopy(load_release_manifest(channel, manifest_dir))
    public = _with_required_defaults(manifest)

    normalized_channel = normalize_channel(str(public.get("channel", "stable")))
    candidate = _candidate_block(public)
    rollout_percent = _rollout_percent(public)
    frozen = _rollout_frozen(public)

    candidate_version = _string(candidate.get("version") or public.get("candidate_version") or public.get("version"))
    candidate_released = _string(
        candidate.get("released") or public.get("candidate_released") or public.get("released")
    )
    candidate_url = _string(candidate.get("url") or public.get("candidate_url") or public.get("url"))

    previous_version = _string(public.get("previous_stable_version") or public.get("version") or candidate_version)
    previous_released = _string(public.get("previous_stable_released") or public.get("released") or candidate_released)
    previous_url = _string(public.get("previous_stable_url") or public.get("url") or candidate_url)

    if normalized_channel == "beta" or (not frozen and rollout_percent >= 100):
        effective_version = candidate_version
        effective_released = candidate_released
        effective_url = candidate_url
    else:
        effective_version = previous_version
        effective_released = previous_released
        effective_url = previous_url

    release_id = _string(public.get("release_id") or candidate.get("release_id") or candidate_version)
    rollout_steps = list(parse_rollout_steps(public.get("rollout_steps")))

    cohort = dict(public.get("cohort") if isinstance(public.get("cohort"), dict) else {})
    cohort.setdefault("algorithm", COHORT_ALGORITHM)
    cohort.setdefault("key", "install_id")
    cohort.setdefault("modulo", 100)

    rollout = dict(public.get("rollout") if isinstance(public.get("rollout"), dict) else {})
    rollout.update(
        {
            "release_id": release_id,
            "percent": rollout_percent,
            "frozen": frozen,
            "cohort": cohort,
        }
    )

    public.update(
        {
            "schema_version": int(public.get("schema_version") or 1),
            "channel": normalized_channel,
            "version": effective_version,
            "released": effective_released,
            "min_supported": _string(public.get("min_supported") or previous_version or VIOLA_VERSION),
            "url": effective_url,
            "release_id": release_id,
            "rollout_percent": rollout_percent,
            "rollout_steps": rollout_steps,
            "frozen": frozen,
            "previous_stable_version": previous_version,
            "previous_stable_released": previous_released,
            "candidate_version": candidate_version,
            "candidate_released": candidate_released,
            "candidate": candidate,
            "cohort": cohort,
            "rollout": rollout,
        }
    )
    _apply_platform_selection(public, platform)
    return public


def _apply_platform_selection(public: dict[str, Any], platform: str | None) -> None:
    """Mutate `public` in place to reflect a platform-specific artifact.

    No-op for None/unrecognized/`REFERENCE_PLATFORM_KEY` — Windows clients
    (and any caller that never passes `platform`) get exactly the manifest
    computed above, unchanged.

    The projection must be TOTAL (#2636). Every URL/hash field this manifest
    exposes is computed from the Windows reference release before we get here,
    so any field left un-rewritten keeps pointing at the Windows installer.
    The desktop client does not read one blessed field: `_effective_manifest_
    version` in utils/update_checker.py resolves `candidate.url` at >=100%
    rollout and on the beta channel, and `previous_stable_url` while a rollout
    is partial or frozen — so leaving either behind hands a Mac or Linux client
    a `.exe`, and (because the matching sha is left behind too) it passes the
    integrity check on the way in. Rewrite or clear EVERY such field; never
    leave a reference-platform value on a non-reference response.

    "Every such field" includes the VERSION fields, not only the URL and hash
    ones: an un-projected version announces the reference release while the
    projected URL serves this platform's own older artifact, which is a
    permanent phantom update rather than a wrong download (see the block below).
    """
    platform_key = normalize_platform_key(platform)
    if not platform_key:
        return
    public["platform"] = platform_key
    if platform_key == REFERENCE_PLATFORM_KEY:
        return

    downloads = public.get("downloads")
    entry = downloads.get(platform_key) if isinstance(downloads, dict) else None
    entry_url = _string(entry.get("url")) if isinstance(entry, dict) else ""

    # `downloads` only ever carries artifacts for the CURRENT release, so there
    # is no per-platform "previous stable" artifact to point at. The honest
    # answer for a non-reference platform is the download page: it keeps a
    # rollout hold-back (partial/frozen) from being handed the Windows binary
    # without pretending we have that platform's older build.
    public["previous_stable_url"] = GENERIC_DOWNLOAD_PAGE_URL

    if not entry_url:
        # No artifact for this platform. Advertise no binary and no hash —
        # a stale reference-platform sha is what let the wrong installer pass
        # verification, so integrity metadata is cleared, not merely ignored.
        public["url"] = GENERIC_DOWNLOAD_PAGE_URL
        public["installer_url"] = GENERIC_DOWNLOAD_PAGE_URL
        public["platform_artifact_available"] = False
        for stale in ("sha256", "installer_sha256", "installer_filename"):
            public.pop(stale, None)
        candidate = public.get("candidate")
        if isinstance(candidate, dict):
            unavailable_candidate = dict(candidate)
            unavailable_candidate["url"] = GENERIC_DOWNLOAD_PAGE_URL
            unavailable_candidate.pop("sha256", None)
            public["candidate"] = unavailable_candidate
        _project_flat_candidate_url(public, GENERIC_DOWNLOAD_PAGE_URL)
        return

    public["url"] = entry_url
    public["installer_url"] = entry_url
    public["platform_artifact_available"] = True
    entry_sha = _string(entry.get("sha256"))
    if entry_sha:
        public["sha256"] = entry_sha
        public["installer_sha256"] = entry_sha
    else:
        # An entry with a URL but no hash must not inherit the Windows hash.
        public.pop("sha256", None)
        public.pop("installer_sha256", None)
    entry_filename = _string(entry.get("filename"))
    if entry_filename:
        public["installer_filename"] = entry_filename
    else:
        public.pop("installer_filename", None)

    # Project the VERSION too, not just the bytes. Every version field above is
    # computed from the Windows reference release, and Viola ships
    # Windows-first: a non-Windows `downloads` entry legitimately lags the
    # top-level version between staggered per-platform releases (that lag is
    # explicitly sanctioned -- see the windows_x64-scoped version rule in
    # scripts/check_update_manifest_entry_coherence.py). Leaving the version
    # un-projected is the third shape of the class this projection exists to
    # close (#338 was `url`, #2636 `candidate.url` + the sha fields, #4228 an
    # entry with a url but no sha): the response hands this platform its own
    # older artifact while announcing the newer reference version. Observed
    # live 2026-08-07 -- ?platform=macos_x64 and ?platform=linux_x86_64 both
    # advertised 1.0.4 while serving the 1.0.3 zip/AppImage, so a client
    # "updated", re-installed the bytes it already had, stayed on 1.0.3, and
    # was re-offered the same update on every subsequent check. A platform has
    # exactly ONE published artifact, so that artifact's version is the only
    # honest answer for every version this response exposes. (An entry with no
    # `version` of its own cannot be projected; the publish writers always set
    # it, and the entry-coherence gate ties it to the filename.)
    entry_version = _string(entry.get("version"))
    if entry_version:
        public["version"] = entry_version
        public["candidate_version"] = entry_version
        # The HOLD-BACK pair too, for the same reason. `previous_stable_url` was
        # already forced to the generic download page above because there is no
        # per-platform PREVIOUS artifact -- which is exactly the point: the only
        # thing a client on this platform can install, on EITHER branch of
        # `_effective_manifest_version`, is the single entry above. A partial or
        # frozen rollout resolves the PREVIOUS pair, so leaving
        # `previous_stable_version` on the Windows value reopens the identical
        # phantom update through the download page. Measured 2026-08-07 with
        # Windows two releases ahead (previous 1.0.4, candidate 1.0.5) against a
        # mac entry at 1.0.3: a held-back client resolved 1.0.4 over 1.0.3 bytes.
        # This one matters most for ALREADY-SHIPPED clients -- they read this
        # manifest with the pre-fix update_checker and cannot correct it
        # themselves, so the server has to be right on its own.
        public["previous_stable_version"] = entry_version

    candidate = public.get("candidate")
    if isinstance(candidate, dict):
        updated_candidate = dict(candidate)
        updated_candidate["url"] = entry_url
        if entry_version:
            updated_candidate["version"] = entry_version
        if entry_sha:
            updated_candidate["sha256"] = entry_sha
        else:
            updated_candidate.pop("sha256", None)
        public["candidate"] = updated_candidate
    _project_flat_candidate_url(public, entry_url)


def _project_flat_candidate_url(public: dict[str, Any], projected_url: str) -> None:
    """Keep the FLAT `candidate_url` in step with the projected `candidate.url`.

    `candidate_url` is the flat twin of `candidate.url`, and three readers
    prefer it over the top-level `url`: `utils/update_checker.py`'s
    `_effective_manifest_version`, and this module's own version resolution and
    `_candidate_block` default (whose own last-resort fallback is the literal
    Windows installer URL). Nothing in this repo writes the key today, so it
    only ever appears on a response because a manifest FILE carried it -- and a
    value carried in from the manifest is by definition the reference-platform
    one, which is precisely the value that must not survive onto a non-Windows
    response.

    Rewritten where it exists rather than unconditionally created: the shipped
    manifests do not carry the key, and inventing it here would change the
    response shape for every platform to fix a field nobody sends.
    """
    if "candidate_url" not in public:
        return
    public["candidate_url"] = projected_url


def _string(value: Any) -> str:
    return str(value or "").strip()


def _rollout_percent(manifest: dict[str, Any]) -> int:
    rollout = manifest.get("rollout")
    nested = rollout.get("percent") if isinstance(rollout, dict) else None
    raw = manifest.get("rollout_percent")
    if raw is None:
        raw = nested if nested is not None else 100
    return normalize_rollout_percent(raw)


def _rollout_frozen(manifest: dict[str, Any]) -> bool:
    rollout = manifest.get("rollout")
    nested = rollout.get("frozen") if isinstance(rollout, dict) else None
    raw = manifest.get("frozen")
    if raw is None:
        raw = nested if nested is not None else False
    return bool(raw)


def _candidate_block(manifest: dict[str, Any]) -> dict[str, Any]:
    candidate = manifest.get("candidate")
    if isinstance(candidate, dict):
        result = dict(candidate)
    else:
        result = {}
    result.setdefault(
        "version",
        _string(manifest.get("candidate_version") or manifest.get("version") or VIOLA_VERSION),
    )
    result.setdefault(
        "released",
        _string(manifest.get("candidate_released") or manifest.get("released") or DEFAULT_RELEASED),
    )
    result.setdefault(
        "url",
        _string(manifest.get("candidate_url") or manifest.get("url") or "https://useviola.com/download/latest.exe"),
    )
    result.setdefault("mandatory", False)
    return result


def _with_required_defaults(manifest: dict[str, Any]) -> dict[str, Any]:
    result = dict(manifest)
    result.setdefault("schema_version", 1)
    result.setdefault("channel", "stable")
    result.setdefault("version", VIOLA_VERSION)
    result.setdefault("released", DEFAULT_RELEASED)
    result.setdefault("min_supported", VIOLA_VERSION)
    result.setdefault("url", "https://useviola.com/download/latest.exe")
    result.setdefault("previous_stable_version", result["version"])
    result.setdefault("previous_stable_released", result["released"])
    result.setdefault("previous_stable_url", result["url"])
    result.setdefault("rollout_percent", 100)
    result.setdefault("rollout_steps", list(DEFAULT_ROLLOUT_STEPS))
    result.setdefault("frozen", False)
    return result


def _default_manifest(channel: str) -> dict[str, Any]:
    normalized_channel = normalize_channel(channel)
    beta = normalized_channel == "beta"
    download_url = (
        "https://useviola.com/download/latest-beta.exe" if beta else "https://useviola.com/download/latest.exe"
    )
    release_id = f"{VIOLA_VERSION}-{DEFAULT_RELEASED}-{normalized_channel}"
    return {
        "schema_version": 1,
        "channel": normalized_channel,
        "version": VIOLA_VERSION,
        "released": DEFAULT_RELEASED,
        "min_supported": VIOLA_VERSION,
        "url": download_url,
        "release_id": release_id,
        "rollout_percent": 100,
        "rollout_steps": list(DEFAULT_ROLLOUT_STEPS),
        "frozen": False,
        "previous_stable_version": VIOLA_VERSION,
        "previous_stable_released": DEFAULT_RELEASED,
        "previous_stable_url": "https://useviola.com/download/latest.exe",
        "cohort": {
            "algorithm": COHORT_ALGORITHM,
            "key": "install_id",
            "modulo": 100,
        },
        "candidate": {
            "version": VIOLA_VERSION,
            "released": DEFAULT_RELEASED,
            "url": download_url,
            "release_id": release_id,
            "mandatory": False,
        },
        "downloads": {
            "windows_x64": {
                "url": download_url,
                "filename": f"ViolaSetup_{VIOLA_VERSION}_{normalized_channel}.exe",
            }
        },
        "native_apply_supported": True,
        "update_apply_boundary": "native_velopack",
        "ramp": [],
    }
