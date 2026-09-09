"""Periodic desktop update checks and manual reinstall prompts.

The checker owns the long-lived update notification state. It does not download
or launch installers; public desktop updates are manual reinstall only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from core.constants import VIOLA_VERSION
from core.logging_config import get_logger
from core.platform import get_cache_dir, platform_download_key

if TYPE_CHECKING:
    from PySide6.QtWidgets import QSystemTrayIcon

logger = get_logger(__name__)

# Phase 1: stable manifest is a Cloudflare Pages function backed by R2 — flippable
# on launch day without a backend deploy. This is the canonical force-update lever.
_DEFAULT_MANIFEST_URL = "https://useviola.com/update/latest.json"
# Phase 3: beta manifest is the rollout-aware backend endpoint for opt-in early-access.
_BETA_MANIFEST_URL = "https://api.useviola.com/api/version/beta.json"
_DEFAULT_DOWNLOAD_URL = "https://useviola.com/download"
# The canonical Windows Velopack feed. This is the value the live manifest
# publishes (ViolaWebsite/functions/update/[filename].js), so it is a true
# default rather than a guess -- the manifest may override it, but a client that
# has not read a manifest yet is not wrong, it is merely un-refreshed.
DEFAULT_VELOPACK_FEED_URL = "https://useviola.com/updates/win"
NATIVE_SELF_UPDATE_APPLY_SUPPORTED = True
UPDATE_APPLY_BOUNDARY = "native_velopack"
_CHECK_DELAY_SECONDS = 30
_MIN_GATE_DELAY_SECONDS = 5
_CHECK_INTERVAL_SECONDS = 4 * 60 * 60
_REMIND_LATER_SECONDS = 24 * 60 * 60
_ESCALATE_BANNER_AFTER_SECONDS = 3 * 24 * 60 * 60
_REQUIRE_DECISION_AFTER_SECONDS = 7 * 24 * 60 * 60
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on", "y"})

# -- Manifest fetch budget + cache ----------------------------------------
#
# A cold first run used to make THREE identical GETs for the same document
# inside 30 seconds (the blocking velopack-feed read, the 5s min-supported
# gate, the 30s routine scheduler) and then two independent 4-hourly loops
# kept re-paying it forever. Nothing remembered a FAILURE, so a machine that
# is offline, on a captive portal, or behind a TLS-inspecting proxy that drops
# packets paid the full connect timeout on every single one of them. Measured
# 2026-08-02 with connect() blackholed in-process: 10.39-10.90s per fetch,
# 31.67s for the three a cold run makes.
#
# The document is the same for every caller, so fetch it once and share it.
_MANIFEST_TTL_SECONDS = 15 * 60
# After a failure, do not re-attempt for a growing cooldown. Capped so a
# machine that comes back online recovers on its own without a restart.
_MANIFEST_FAILURE_COOLDOWN_SECONDS = 60
_MANIFEST_FAILURE_COOLDOWN_MAX_SECONDS = 30 * 60
# Split the old single 10s scalar: a TCP/TLS connect to a CDN edge that has not
# completed in 5s is not going to, and the connect leg is the one an offline
# machine actually pays. The read budget stays generous for a slow-but-alive
# link. Note httpx applies the timeout PER REQUEST and this call follows
# redirects, so the scalar form was also multiplied by any redirect chain.
_MANIFEST_CONNECT_TIMEOUT_SECONDS = 5.0
_MANIFEST_READ_TIMEOUT_SECONDS = 10.0

UpdateCallback = Callable[[dict[str, object]], None]


class _ManifestFetchCache:
    """Process-wide cache in front of the single update-manifest network call.

    Holds three things per manifest URL:

    * a short-TTL copy of the parsed document, so concurrent/near-in-time
      callers share one round trip;
    * a failure cooldown with exponential backoff, so an unreachable network
      is paid once rather than once per caller;
    * a last-known-good document that survives TTL expiry, used only by readers
      that must never initiate a fetch (see ``cached_velopack_feed_url``).

    Only the parsed DOCUMENT is cached -- never the computed result -- so every
    caller still re-derives ``available`` / ``required`` / ``min_supported``
    against live settings and the live running version.
    """

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._documents: dict[str, tuple[float, dict[str, object]]] = {}
        self._last_good: dict[str, dict[str, object]] = {}
        self._failures: dict[str, tuple[float, int, BaseException]] = {}

    def fetch_lock(self, url: str) -> threading.Lock:
        """Serialize fetches for one URL so callers never stampede the network."""
        with self._state_lock:
            lock = self._fetch_locks.get(url)
            if lock is None:
                lock = threading.Lock()
                self._fetch_locks[url] = lock
            return lock

    def fresh_document(self, url: str) -> dict[str, object] | None:
        with self._state_lock:
            entry = self._documents.get(url)
            if entry is None:
                return None
            fetched_at, document = entry
            if time.monotonic() - fetched_at >= _MANIFEST_TTL_SECONDS:
                return None
            return document

    def last_good_document(self, url: str) -> dict[str, object] | None:
        """The most recent successful document, ignoring TTL. Never fetches."""
        with self._state_lock:
            return self._last_good.get(url)

    def cooldown_error(self, url: str) -> BaseException | None:
        """The remembered failure while this URL is still in backoff, else None."""
        with self._state_lock:
            entry = self._failures.get(url)
            if entry is None:
                return None
            failed_at, consecutive, exc = entry
            cooldown = min(
                _MANIFEST_FAILURE_COOLDOWN_SECONDS * (2 ** max(0, consecutive - 1)),
                _MANIFEST_FAILURE_COOLDOWN_MAX_SECONDS,
            )
            if time.monotonic() - failed_at >= cooldown:
                return None
            return exc

    def record_success(self, url: str, document: dict[str, object]) -> None:
        with self._state_lock:
            self._documents[url] = (time.monotonic(), document)
            self._last_good[url] = document
            self._failures.pop(url, None)

    def record_failure(self, url: str, exc: BaseException) -> None:
        with self._state_lock:
            previous = self._failures.get(url)
            consecutive = (previous[1] + 1) if previous is not None else 1
            self._failures[url] = (time.monotonic(), consecutive, exc)

    def reset(self) -> None:
        with self._state_lock:
            self._fetch_locks.clear()
            self._documents.clear()
            self._last_good.clear()
            self._failures.clear()


_MANIFEST_CACHE = _ManifestFetchCache()


def reset_manifest_cache() -> None:
    """Drop every cached manifest document and failure cooldown.

    Exists so a test (and the autouse fixture in ``tests/conftest.py``) can start
    from a cold cache, and so a caller can deliberately discard a stale document.
    """
    _MANIFEST_CACHE.reset()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_VALUES


def _beta_updates_enabled() -> bool:
    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("early_access_updates", False))
    except Exception:
        return False


def _resolve_manifest_url(manifest_url: str | None = None) -> str:
    if manifest_url:
        return manifest_url
    env_url = os.environ.get("VIOLA_UPDATE_MANIFEST_URL")
    if env_url:
        return env_url
    return _BETA_MANIFEST_URL if _beta_updates_enabled() else _DEFAULT_MANIFEST_URL


def _candidate_block(data: dict[str, object]) -> dict[str, object]:
    candidate = data.get("candidate")
    return candidate if isinstance(candidate, dict) else {}


def _effective_manifest_version(data: dict[str, object], install_id: str) -> tuple[str, str]:
    """Return the version/url this install should see from an additive manifest.

    Phase 3 cohort-aware selection: handles `rollout_percent`, `frozen`,
    `previous_stable_version`, `candidate.version`, and the beta channel.
    The `min_supported` floor is computed separately by `check_for_update`
    and is never bypassed by this logic.
    """
    from updates.rollout import is_in_rollout, normalize_rollout_percent

    candidate = _candidate_block(data)
    rollout = data.get("rollout")
    rollout = rollout if isinstance(rollout, dict) else {}
    channel = str(data.get("channel") or "stable").strip().lower()
    top_level_version = str(data.get("version", "")).strip()
    top_level_url = str(data.get("url", "")).strip()
    candidate_version = str(candidate.get("version") or data.get("candidate_version") or top_level_version).strip()
    candidate_url = str(candidate.get("url") or data.get("candidate_url") or top_level_url).strip()
    previous_version = str(data.get("previous_stable_version") or top_level_version).strip()
    previous_url = str(data.get("previous_stable_url") or top_level_url).strip()

    if channel == "beta":
        return candidate_version or top_level_version, candidate_url or top_level_url

    release_id = str(data.get("release_id") or candidate.get("release_id") or candidate_version).strip()
    raw_percent = data.get("rollout_percent")
    if raw_percent is None:
        raw_percent = rollout.get("percent", 100)
    raw_frozen = data.get("frozen")
    if raw_frozen is None:
        raw_frozen = rollout.get("frozen", False)
    rollout_percent = normalize_rollout_percent(raw_percent)
    frozen = bool(raw_frozen)
    if not release_id or frozen:
        return previous_version or top_level_version, previous_url or top_level_url
    if rollout_percent >= 100:
        return candidate_version or top_level_version, candidate_url or top_level_url
    if install_id and is_in_rollout(install_id, release_id, rollout_percent):
        return candidate_version or top_level_version, candidate_url or top_level_url
    return previous_version or top_level_version, previous_url or top_level_url


def _install_id_for_update_check() -> str:
    try:
        from telemetry.install_id import get_or_create_install_id

        return get_or_create_install_id()
    except Exception:
        logger.debug("Update check could not read install id")
        return ""


def _user_minimum_version() -> str:
    """Read the user-pinned minimum version from SettingsManager.

    Parity with Claude Code's ``shouldSkipVersion`` (``autoUpdater.ts:140-158``):
    a user can pin the lowest release they want to accept so a channel-switch
    or a re-flash doesn't silently downgrade them. Empty string means
    "no user floor".

    The ``min_supported`` floor in the manifest (security CVE response lever)
    is ALWAYS stronger than this and is enforced separately in
    ``check_for_update``.
    """
    try:
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get("update_minimum_version", "")
    except Exception:
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""


def safe_update_download_url(value: object, fallback: str = _DEFAULT_DOWNLOAD_URL) -> str:
    """Return ``value`` only when it is a well-formed ``https://`` URL; else ``fallback``.

    The update manifest is fetched over TLS but carries no end-to-end signature,
    so any URL inside it is untrusted input (SEC-016/SEC-006): a compromised or
    malicious manifest must never be able to point the forced-update modal — or
    any other consumer that hands the URL to the OS — at a non-https scheme
    (``file:``, ``javascript:``, custom protocol handlers) or a credential-
    embedding URL. Mirrors ``desktop_updater._installer_url``'s https-only rule,
    but falls back to the trusted download page instead of raising because the
    forced-update modal must still give the user a way forward.
    """
    from urllib.parse import urlparse

    candidate = str(value or "").strip()
    if not candidate or any(ord(ch) < 33 or ord(ch) == 127 for ch in candidate):
        if candidate:
            logger.warning("Rejecting update download URL with unsafe characters; using default download page")
        return fallback
    try:
        parsed = urlparse(candidate)
        _ = parsed.port  # Accessing .port validates malformed port values.
    except ValueError:
        logger.warning("Rejecting malformed update download URL; using default download page")
        return fallback
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        logger.warning(
            "Rejecting update download URL with disallowed scheme %r; using default download page",
            parsed.scheme,
        )
        return fallback
    if parsed.username or parsed.password:
        logger.warning("Rejecting update download URL with embedded credentials; using default download page")
        return fallback
    return candidate


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '1.2.3' into a comparable tuple."""
    parts: list[int] = []
    for segment in v.strip().lstrip("v").split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


def _normalise_manifest(data: object) -> dict[str, object]:
    """Return the manifest payload from raw or ResponseEnvelope JSON."""
    if not isinstance(data, dict):
        return {}
    if "data" in data and isinstance(data["data"], dict):
        return data["data"]
    return data


def _default_state_path() -> Path:
    return get_cache_dir() / "updates" / "state.json"


def _manifest_bool(data: dict[str, object], *keys: str) -> bool:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}:
            return True
    return False


def _manifest_int(data: dict[str, object], key: str, default: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def check_for_update(manifest_url: str | None = None, *, force_refresh: bool = False) -> dict[str, object]:
    """Check the update manifest once and return version comparison details.

    Uses Phase 3 cohort-aware version selection (rollout %, frozen, beta candidate,
    previous_stable_version), and Phase 1's `min_supported` floor as the absolute
    force-update lever (never bypassed by rollout logic).

    The manifest DOCUMENT is served from a short-TTL process cache shared by every
    caller, and a failed fetch puts the URL into a backing-off cooldown during which
    this raises the remembered error without touching the network. Only the network
    leg is cached: the result is always re-derived against live settings and the live
    running version. ``force_refresh=True`` bypasses both the TTL and the cooldown --
    use it for an explicit, user-initiated "check for updates now", never for a
    background poll.
    """
    url = _resolve_manifest_url(manifest_url)
    # #338: tell the server which platform is asking. A platform-aware
    # manifest endpoint (updates.manifest.build_public_manifest) selects a
    # matching `downloads` entry server-side; a static-manifest endpoint
    # (e.g. the Cloudflare Pages default) just ignores the unknown param, so
    # this is always safe to send.
    platform_key = platform_download_key()
    data = None if force_refresh else _MANIFEST_CACHE.fresh_document(url)
    if data is None:
        # Held across the fetch on purpose: a second caller arriving mid-flight
        # waits for the in-flight answer instead of opening its own connection,
        # which is what collapses the cold-start burst into one round trip.
        with _MANIFEST_CACHE.fetch_lock(url):
            data = None if force_refresh else _MANIFEST_CACHE.fresh_document(url)
            if data is None:
                if not force_refresh:
                    cooled = _MANIFEST_CACHE.cooldown_error(url)
                    if cooled is not None:
                        logger.debug("Update manifest fetch skipped; still in failure cooldown for %s", url)
                        raise cooled
                try:
                    resp = httpx.get(
                        url,
                        params={"platform": platform_key},
                        timeout=httpx.Timeout(
                            _MANIFEST_READ_TIMEOUT_SECONDS,
                            connect=_MANIFEST_CONNECT_TIMEOUT_SECONDS,
                        ),
                        follow_redirects=True,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                except Exception as exc:
                    _MANIFEST_CACHE.record_failure(url, exc)
                    raise
                data = _normalise_manifest(payload)
                _MANIFEST_CACHE.record_success(url, data)

    # Cohort-aware version selection plus installer integrity metadata.
    remote_version, download_url = _effective_manifest_version(data, _install_id_for_update_check())

    # Platform-aware artifact selection (#338), client-side defense in depth.
    # The manifest's `downloads` block may already be platform-projected by a
    # server that understood the `platform` param above, or it may be an
    # unfiltered static manifest whose top-level url/sha are Windows-only
    # (windows_x64 is the only key most manifests populate today). On any
    # non-Windows platform, prefer that platform's own `downloads` entry —
    # when one is published — over the top-level fields, so this client never
    # offers itself the wrong platform's installer. This branch is a no-op
    # for windows_x64: Windows behavior is unchanged.
    platform_sha256 = ""
    # The full platform-projected artifact entry (url + sha256 + signing_team_id
    # + notarized + size). The macOS in-app apply adapter (utils.macos_updater.
    # MacUpdater, #2634) needs the team-id + notarized fields — which only live
    # in this downloads entry — to build a faithful, fail-closed apply artifact,
    # so we carry the whole entry forward on the result. Never populated for
    # windows_x64 (Velopack owns that leg), so Windows behavior is unchanged.
    platform_artifact: dict[str, object] = {}
    # Fail CLOSED when this platform has no artifact of its own (#2636). The
    # branch below used to only *upgrade* download_url when a matching entry
    # existed; with no entry it fell through to whatever `_effective_manifest_
    # version` resolved, which on any non-Windows platform is the Windows
    # reference URL (top-level / candidate.url / previous_stable_url), and
    # `sha256` fell back to the matching Windows hash — so the wrong-platform
    # installer downloaded AND verified cleanly. An Apple Silicon build asking
    # for `macos_arm64` before an arm64 artifact is published is exactly that
    # case. Absence must degrade to the download page, never to another
    # platform's binary.
    platform_artifact_missing = False
    if platform_key != "windows_x64":
        downloads = data.get("downloads")
        platform_entry = downloads.get(platform_key) if isinstance(downloads, dict) else None
        entry_url = ""
        if isinstance(platform_entry, dict):
            platform_artifact = dict(platform_entry)
            entry_url = str(platform_entry.get("url") or "").strip()
        if entry_url:
            download_url = entry_url
            entry_version = str(platform_entry.get("version") or "").strip()
            if entry_version:
                # Adopt the entry's VERSION for the same reason we just adopted
                # its URL. `remote_version` above was resolved from the
                # reference (Windows) release, and Viola ships Windows-first, so
                # a mac/linux entry legitimately lags it. Keeping the reference
                # version while installing this platform's older artifact tells
                # a client an update exists, hands it the bytes it is already
                # running, and re-offers the same update on every check --
                # a phantom-update loop, not a wrong download. Server-side
                # projection (updates/manifest.py) fixes this at the source;
                # this branch is the same defense-in-depth that made the client
                # prefer the entry's url/sha, and it also covers an unfiltered
                # static manifest that was never platform-projected at all.
                remote_version = entry_version
            platform_sha256 = str(platform_entry.get("sha256") or "").strip()
            if not platform_sha256:
                # #4228: a published entry that carries a url but NO sha256 is
                # the second shape of the same leak #2636 closed. #2636 only
                # covered the entry being absent entirely; an entry with a url
                # and no hash left `platform_sha256` empty and fell through to
                # the top-level `sha256`, which describes the WINDOWS reference
                # installer -- so a Linux/macOS client downloaded its own
                # platform's artifact and then verified it against Windows'
                # hash. This is not hypothetical: on 2026-07-30 the live
                # manifest carried a linux_x86_64 entry whose hash matched
                # neither the served bytes nor the repo manifest. The
                # top-level hash is never a valid substitute off the reference
                # platform, so refuse it rather than inherit it -- an empty
                # `sha256` makes the caller skip verification, while a WRONG
                # one makes a correct download look corrupt (or, with the
                # platforms transposed, the wrong binary look correct).
                logger.warning(
                    "Update manifest's %s artifact carries no sha256; refusing to fall back to the "
                    "top-level (reference-platform) hash, which describes a different platform's installer",
                    platform_key,
                )
        else:
            platform_artifact_missing = True
            platform_artifact = {}
            download_url = _DEFAULT_DOWNLOAD_URL
            platform_sha256 = ""
            logger.info(
                "Update manifest carries no %s artifact; offering the download page instead of another platform's installer",
                platform_key,
            )

    # The manifest's TOP-LEVEL sha256/installer_sha256 describe the reference
    # (Windows) installer -- every publish writer fills them from
    # downloads.windows_x64. They are therefore a valid default on windows_x64
    # and on NO other platform, so this one predicate gates every place the
    # top-level hash could be adopted below (#4228).
    top_level_sha_applies = platform_key == "windows_x64"

    if not download_url:
        download_url = str(
            data.get("url") or data.get("download_url") or data.get("installer_url") or _DEFAULT_DOWNLOAD_URL
        ).strip()
    # SEC-016/SEC-006: the manifest is TLS-trusted but unsigned — sanitize the
    # download URL at the source so every consumer gets an https-only URL.
    download_url = safe_update_download_url(download_url)
    min_supported = str(data.get("min_supported", "")).strip()
    # Server-side max-version cap (S10-UPDATE-002 / parity with Claude
    # Code's getMaxVersion in autoUpdater.ts:108-138). When the cloud team
    # publishes ``max_version`` to contain a known-bad release without a
    # redeploy, the routine update path stops offering anything above it,
    # and clients already running a version above the cap are told a
    # known issue affects their build. ``min_supported`` (security)
    # always wins over ``max_version`` (containment).
    max_version = str(data.get("max_version", "")).strip()
    max_version_message = str(data.get("max_version_message", "")).strip()
    frozen = _manifest_bool(data, "frozen")
    # User-side minimum version (parity with shouldSkipVersion).
    user_min = _user_minimum_version()
    current_parsed = _parse_version(VIOLA_VERSION)
    max_version_issue = bool(max_version and current_parsed > _parse_version(max_version))
    result: dict[str, object] = {
        "available": False,
        "current_version": VIOLA_VERSION,
        "latest_version": remote_version,
        "min_supported": min_supported,
        "max_version": max_version,
        "max_version_message": max_version_message,
        "max_version_issue": max_version_issue,
        "unsupported": False,
        "url": download_url,
        # Never inherit the reference platform's hash on a platform that has no
        # artifact — that inheritance is what let a Windows .exe pass this
        # client's own integrity check on a Mac (#2636) — and never inherit it
        # on a non-Windows platform whose entry simply omits its own hash
        # (#4228, the second shape of the same leak).
        "sha256": (
            ""
            if platform_artifact_missing
            else (
                platform_sha256
                or (
                    str(data.get("sha256", "") or data.get("installer_sha256", "")).strip()
                    if top_level_sha_applies
                    else ""
                )
            )
        ),
        "platform": platform_key,
        "signing_thumbprint": str(data.get("signing_thumbprint", "")).strip(),
        "signing_subject": str(data.get("signing_subject", "")).strip(),
        "signing_issuer_fragment": str(data.get("signing_issuer_fragment", "")).strip(),
        "required": False,
        "native_apply_supported": NATIVE_SELF_UPDATE_APPLY_SUPPORTED,
        "update_apply_boundary": UPDATE_APPLY_BOUNDARY,
        "velopack_feed_url": str(data.get("velopack_feed_url", "") or "").strip(),
    }
    if user_min:
        result["user_minimum_version"] = user_min
    # Carry the platform artifact entry so the macOS apply adapter can build a
    # faithful artifact (signing_team_id / notarized live only here). Additive
    # and non-Windows only — absent for windows_x64.
    if platform_artifact:
        result["platform_artifact"] = platform_artifact
    # Additive and non-Windows only, exactly like `platform_artifact` above:
    # windows_x64 is the reference platform and its response shape must not
    # change because this feature exists. False means "this platform has no
    # artifact in the feed, so `url` is the download page and `sha256` is
    # deliberately empty" (#2636).
    if platform_key != "windows_x64":
        result["platform_artifact_available"] = not platform_artifact_missing

    optional_keys = {
        "released": "released",
        "min_supported": "min_supported",
        "size_bytes": "size_bytes",
        "notes_url": "notes_url",
        "rollout_percent": "rollout_percent",
        "signing_thumbprint": "signing_thumbprint",
        "signing_subject": "signing_subject",
        "signing_issuer_fragment": "signing_issuer_fragment",
    }
    for source_key, result_key in optional_keys.items():
        if source_key in data:
            result[result_key] = data[source_key]
    # `sha256` field is canonical; data may carry it as `sha256` or `installer_sha256`.
    # platform_sha256 (this platform's own downloads entry) wins over the
    # top-level fields, which may describe a different platform's artifact.
    # When this platform has NO artifact the top-level hash describes the
    # reference (Windows) installer, so it must not be adopted at all (#2636) —
    # this re-assignment is downstream of the result dict and would otherwise
    # put the leaked hash straight back. The same reasoning bars the top-level
    # fallback on ANY non-reference platform, artifact present or not (#4228):
    # off windows_x64 the top-level hash is always some other platform's.
    if not platform_artifact_missing:
        sha_value = platform_sha256 or (
            str(data.get("sha256") or data.get("installer_sha256") or "").strip() if top_level_sha_applies else ""
        )
        if sha_value:
            result["sha256"] = sha_value
    if "frozen" in data:
        result["frozen"] = frozen
    if "freeze_reason" in data:
        result["freeze_reason"] = data.get("freeze_reason", "")
    unsupported = bool(min_supported and _parse_version(VIOLA_VERSION) < _parse_version(min_supported))
    required = _manifest_bool(data, "required") or unsupported
    result["unsupported"] = unsupported
    result["required"] = required
    if "critical" in data or "force" in data or required:
        result["critical"] = _manifest_bool(data, "critical", "force") or required

    # `mandatory` is the publisher's commitment that this release was classified
    # into the closed safety/brick force-update class (publish-update SKILL.md
    # step 8). It is surfaced here as a distinct, structured signal so the apply
    # policy (updates.update_policy) can resolve OFFERED vs FORCED without
    # re-deriving it from min_supported/critical. It is carried on the manifest
    # top level or under the candidate block; project either spelling. The
    # accompanying reason(s) are passed through verbatim for the policy's closed-
    # allowlist check — an unrecognised or absent reason fails closed to OFFERED.
    candidate_block = _candidate_block(data)
    mandatory = _manifest_bool(data, "mandatory") or _manifest_bool(candidate_block, "mandatory")
    result["mandatory"] = mandatory
    mandatory_reason = data.get("mandatory_reason")
    if mandatory_reason is None:
        mandatory_reason = data.get("mandatory_reasons")
    if mandatory_reason is None:
        mandatory_reason = candidate_block.get("mandatory_reason")
    if mandatory_reason is None:
        mandatory_reason = candidate_block.get("mandatory_reasons")
    if mandatory_reason is not None:
        result["mandatory_reason"] = mandatory_reason

    if not remote_version and not min_supported:
        result["reason"] = "no_version"
        return result
    if not remote_version:
        return result

    remote_parsed = _parse_version(remote_version)
    is_newer = remote_parsed > _parse_version(VIOLA_VERSION)
    if frozen and is_newer and not required:
        result["reason"] = "frozen"
        return result

    rollout_percent = _manifest_int(data, "rollout_percent", 100)
    if is_newer and rollout_percent <= 0 and not required:
        result["reason"] = "rollout_paused"
        return result

    # Server-side max-version cap — suppress the routine update path when
    # the candidate would push the user above a known-bad version. Security
    # floor (min_supported / required) still wins.
    if max_version and is_newer and remote_parsed > _parse_version(max_version) and not required:
        result["reason"] = "max_version"
        return result

    # User-side minimum-version floor — protects against channel-switch
    # downgrades. min_supported (security) overrides this.
    if user_min and is_newer and remote_parsed < _parse_version(user_min) and not required:
        result["reason"] = "below_user_minimum"
        return result

    result["available"] = is_newer or required
    return result


def cached_velopack_feed_url(manifest_url: str | None = None) -> str:
    """Return the Velopack feed URL, WITHOUT ever initiating a network fetch.

    The feed URL is only needed when the user actually applies an update (the
    banner's "Restart now" reaches ``VelopackUpdater._build_manager``), which is
    minutes-to-days after launch and always long after the background checkers
    have populated the manifest cache. Reading it eagerly at startup used to put
    a synchronous ``httpx.get`` on the main thread ahead of ``window.show()``, so
    the first paint of a brand-new install waited on the network for a string
    (see ``viola_qt._attach_update_scheduler``). This reader is deliberately
    cache-only: a cold cache yields the canonical default rather than a stall.

    A manifest is TLS-trusted but unsigned, so a non-``https`` feed URL out of it
    is rejected in favour of the trusted default -- a Velopack feed is where
    signed binaries come from, and downgrading it to plain http would be a
    trivial MITM apply vector (same rule as ``VelopackUpdater.__init__``).
    """
    url = _resolve_manifest_url(manifest_url)
    document = _MANIFEST_CACHE.last_good_document(url)
    if not isinstance(document, dict):
        return DEFAULT_VELOPACK_FEED_URL
    candidate = str(document.get("velopack_feed_url") or "").strip()
    if not candidate:
        return DEFAULT_VELOPACK_FEED_URL
    if not candidate.lower().startswith("https://"):
        logger.warning(
            "Ignoring non-https velopack_feed_url %r from the update manifest; using the default feed",
            candidate,
        )
        return DEFAULT_VELOPACK_FEED_URL
    return candidate


class UpdateStateStore:
    """Small JSON store for update reminder state."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_state_path()
        self._lock = threading.Lock()

    def load(self) -> dict[str, object]:
        with self._lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}

    def save(self, state: dict[str, object]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def update(self, **values: object) -> dict[str, object]:
        state = self.load()
        state.update(values)
        self.save(state)
        return state


class UpdateCheckScheduler:
    """Periodic update checker with manual reinstall reminder state."""

    def __init__(
        self,
        *,
        tray_icon: QSystemTrayIcon | None = None,
        manifest_url: str | None = None,
        event_callback: UpdateCallback | None = None,
        state_store: UpdateStateStore | None = None,
        initial_delay_seconds: int = _CHECK_DELAY_SECONDS,
        interval_seconds: int = _CHECK_INTERVAL_SECONDS,
        remind_later_seconds: int = _REMIND_LATER_SECONDS,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.tray_icon = tray_icon
        # Resolve through the one resolver so a directly-constructed scheduler
        # honours the early-access (beta) channel setting, not just the env var.
        self.manifest_url = _resolve_manifest_url(manifest_url)
        self.event_callback = event_callback
        self.state_store = state_store or UpdateStateStore()
        self.initial_delay_seconds = initial_delay_seconds
        self.interval_seconds = interval_seconds
        self.remind_later_seconds = remind_later_seconds
        self.time_fn = time_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_result: dict[str, object] | None = None

    def start(self) -> None:
        """Start periodic checks in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="update-checker")
        self._thread.start()
        logger.debug(
            "Update checker scheduled (delay=%ds, interval=%ds)",
            self.initial_delay_seconds,
            self.interval_seconds,
        )

    def stop(self) -> None:
        """Stop future periodic checks."""
        self._stop_event.set()

    def run_check_once(self) -> dict[str, object]:
        """Perform one check and emit update events as needed.

        Honours the ``auto_update_check_enabled`` opt-out (and the
        ``VIOLA_DISABLE_UPDATE_CHECK`` env override) via
        ``is_update_notification_enabled``: when the user has turned the
        periodic version check off, this makes NO network call and emits
        nothing. The check is re-read on every tick, so toggling the setting
        takes effect without a restart. The separate ``min_supported`` safety
        floor (``schedule_min_supported_gate``) is deliberately NOT gated here
        - that is the CVE / brick-fix response lever and stays on.
        """
        if not is_update_notification_enabled():
            logger.debug("Periodic update check skipped by user opt-out (auto_update_check_enabled)")
            return {}
        result = check_for_update(self.manifest_url)
        self._latest_result = result
        remote_version = str(result.get("latest_version", ""))

        # Surface "your build has a known issue, please roll back" any time
        # the manifest's max_version cap is below the running version. This
        # event must fire even if the manifest is otherwise "up to date"
        # (the routine update path is suppressed but the user still needs
        # to know to roll back). Parity with NativeAutoUpdater.tsx:90-96.
        if result.get("max_version_issue"):
            self._emit(
                "max_version_issue",
                {
                    "current_version": str(result.get("current_version", "")),
                    "max_version": str(result.get("max_version", "")),
                    "max_version_message": str(result.get("max_version_message", "")),
                    "latest_version": remote_version,
                },
            )

        if not remote_version:
            logger.debug("Update check: no version in manifest response")
            return result

        if not result.get("available"):
            logger.debug(
                "Up to date or unavailable (local=%s, remote=%s)",
                VIOLA_VERSION,
                remote_version,
            )
            return result

        logger.info("New version available: %s (current: %s)", remote_version, VIOLA_VERSION)
        self._remember_available_version(result)
        payload = self._event_payload(result)

        if not self._is_reminder_suppressed(payload):
            self._emit("available", payload)
            self._show_tray_notification(remote_version)

        return result

    def remind_later(self) -> None:
        """Suppress non-critical banners for the reminder window."""
        until = self.time_fn() + self.remind_later_seconds
        self.state_store.update(remind_until=until)
        self._emit("remind_later", {"remind_until": until})

    def stage_install_on_quit(self) -> bool:
        """Manual reinstall builds do not stage installers."""
        self.state_store.update(
            install_on_quit=False,
            native_apply_supported=NATIVE_SELF_UPDATE_APPLY_SUPPORTED,
            update_apply_boundary=UPDATE_APPLY_BOUNDARY,
        )
        return False

    def apply_staged_update_if_requested(self) -> bool:
        """Manual reinstall builds never auto-launch staged installers."""
        self.state_store.update(
            install_on_quit=False,
            native_apply_supported=NATIVE_SELF_UPDATE_APPLY_SUPPORTED,
            update_apply_boundary=UPDATE_APPLY_BOUNDARY,
        )
        return False

    def apply_staged_update(self) -> bool:
        """Manual reinstall builds never auto-launch staged installers."""
        self.state_store.update(
            install_on_quit=False,
            native_apply_supported=NATIVE_SELF_UPDATE_APPLY_SUPPORTED,
            update_apply_boundary=UPDATE_APPLY_BOUNDARY,
        )
        return False

    def _run_loop(self) -> None:
        if self._stop_event.wait(max(0, self.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_check_once()
            except httpx.HTTPStatusError as exc:
                logger.debug("Update check HTTP error: %s", exc.response.status_code)
            except Exception as exc:
                logger.debug("Update check failed (non-critical): %s", exc)
                self._emit("error", {"message": str(exc)})
            if self._stop_event.wait(max(1, self.interval_seconds)):
                return

    def _remember_available_version(self, result: dict[str, object]) -> None:
        remote_version = str(result.get("latest_version", ""))
        state = self.state_store.load()
        if state.get("available_version") == remote_version and state.get("first_seen_at"):
            return
        self.state_store.update(
            available_version=remote_version,
            first_seen_at=self.time_fn(),
            remind_until=0,
            install_on_quit=False,
        )

    def _event_payload(self, result: dict[str, object]) -> dict[str, object]:
        state = self.state_store.load()
        first_seen = float(state.get("first_seen_at") or self.time_fn())
        age_seconds = max(0.0, self.time_fn() - first_seen)
        critical = bool(result.get("critical"))
        if critical:
            escalation = "forced"
        elif age_seconds >= _REQUIRE_DECISION_AFTER_SECONDS:
            escalation = "required"
        elif age_seconds >= _ESCALATE_BANNER_AFTER_SECONDS:
            escalation = "nudge"
        else:
            escalation = "normal"
        payload = dict(result)
        payload.update(
            {
                "first_seen_at": first_seen,
                "available_age_days": round(age_seconds / 86400, 2),
                "escalation": escalation,
            }
        )
        return payload

    def _is_reminder_suppressed(self, payload: dict[str, object]) -> bool:
        if payload.get("escalation") in {"required", "forced"}:
            return False
        state = self.state_store.load()
        try:
            return self.time_fn() < float(state.get("remind_until") or 0)
        except (TypeError, ValueError):
            return False

    def _emit(self, event_type: str, payload: dict[str, object]) -> None:
        event = dict(payload)
        event["type"] = event_type
        if self.event_callback is not None:
            try:
                self.event_callback(event)
            except Exception as exc:
                logger.debug("Update event callback failed: %s", exc)

    def _show_tray_notification(self, remote_version: str) -> None:
        if self.tray_icon is None:
            return
        try:
            from PySide6.QtWidgets import QSystemTrayIcon

            self.tray_icon.showMessage(
                "Viola Update Available",
                "Version %s is available. Download the latest installer from useviola.com/download." % remote_version,
                QSystemTrayIcon.MessageIcon.Information,
                10000,
            )
        except Exception:
            logger.debug("Could not show tray notification for update")


def _do_check(
    manifest_url: str,
    tray_icon: QSystemTrayIcon | None,
    event_callback: UpdateCallback | None = None,
) -> None:
    """Perform one background version check."""
    scheduler = UpdateCheckScheduler(
        tray_icon=tray_icon,
        manifest_url=manifest_url,
        event_callback=event_callback,
        initial_delay_seconds=0,
        interval_seconds=_CHECK_INTERVAL_SECONDS,
    )
    try:
        scheduler.run_check_once()
    except httpx.HTTPStatusError as exc:
        logger.debug("Update check HTTP error: %s", exc.response.status_code)
    except Exception as exc:
        logger.debug("Update check failed (non-critical): %s", exc)


def is_update_notification_enabled(settings_manager: object | None = None) -> bool:
    """Phase 1 opt-out: routine update notifications can be disabled via env var or setting."""
    if _env_truthy("VIOLA_DISABLE_UPDATE_CHECK"):
        return False

    manager = settings_manager
    if manager is None:
        try:
            from ui.settings_manager import get_settings_manager

            manager = get_settings_manager()
        except Exception as exc:
            logger.debug("Update notification setting unavailable; defaulting enabled: %s", exc)
            return True

    try:
        raw_value = manager.get("auto_update_check_enabled", True)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("Update notification setting read failed; defaulting enabled: %s", exc)
        return True

    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() not in {"0", "false", "no", "off", "n"}
    return bool(raw_value)


def _show_update_notification(tray_icon: QSystemTrayIcon | None, result: dict[str, object]) -> bool:
    """Show the optional tray notification for a non-critical update."""
    if tray_icon is None:
        return False

    remote_version = str(result.get("latest_version", ""))
    title = "Viola Update Available"
    message = "Version %s is available (you have %s). Visit useviola.com to update." % (
        remote_version,
        VIOLA_VERSION,
    )

    try:
        from PySide6.QtWidgets import QSystemTrayIcon

        tray_icon.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            10000,
        )
        return True
    except ImportError:
        try:
            tray_icon.showMessage(title, message)  # type: ignore[call-arg]
            return True
        except Exception:
            logger.debug("Could not show tray notification for update")
            return False
    except Exception:
        logger.debug("Could not show tray notification for update")
        return False


def _sleep_then_repeat(
    delay_seconds: float,
    repeat_interval_seconds: float | None,
    fn: Callable[[], None],
) -> None:
    """Sleep, run fn, optionally repeat. Used by Phase 1 fire-and-forget schedulers."""
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    fn()
    if not repeat_interval_seconds or repeat_interval_seconds <= 0:
        return
    while True:
        time.sleep(repeat_interval_seconds)
        try:
            fn()
        except Exception as exc:
            logger.debug("Recurring update check raised: %s", exc)


def _do_one_notification_check(
    manifest_url: str,
    tray_icon: QSystemTrayIcon | None,
    settings_manager: object | None,
) -> None:
    """One-shot notification check — Phase 1 contract: tray balloon if newer version exists."""
    if not is_update_notification_enabled(settings_manager):
        logger.debug("Update notification check skipped by user setting or environment")
        return

    try:
        result = check_for_update(manifest_url)
    except httpx.HTTPStatusError as exc:
        logger.debug("Update check HTTP error: %s", exc.response.status_code)
        return
    except Exception as exc:
        logger.debug("Update check failed (non-critical): %s", exc)
        return

    if result.get("available"):
        _show_update_notification(tray_icon, result)


def _do_min_supported_gate(
    manifest_url: str,
    on_required_update: UpdateCallback,
) -> None:
    """One-shot min_supported gate — Phase 1 contract: fire callback if current < min_supported."""
    try:
        result = check_for_update(manifest_url)
    except Exception as exc:
        logger.debug("Min-supported gate check failed (non-critical): %s", exc)
        return
    if result.get("unsupported") or result.get("required"):
        try:
            on_required_update(result)
        except Exception as exc:
            logger.debug("Required-update callback raised: %s", exc)


def schedule_update_check(
    tray_icon: QSystemTrayIcon | None = None,
    manifest_url: str | None = None,
    *,
    event_callback: UpdateCallback | None = None,
    settings_manager: object | None = None,
    delay_seconds: float | None = None,
    repeat_interval_seconds: float | None = None,
    interval_hours: float | None = None,
    initial_delay_seconds: int | None = None,
) -> threading.Thread | UpdateCheckScheduler | None:
    """Schedule background update checks.

    Two call shapes are supported:

    1. Phase 1 fire-and-forget tray notification (used by viola_qt.py):
       `schedule_update_check(tray_icon=..., settings_manager=..., delay_seconds=0,
       repeat_interval_seconds=None)`. Returns the daemon thread, or `None` when the
       user has opted out of routine update notifications. This honours
       `VIOLA_DISABLE_UPDATE_CHECK` and the `auto_update_check_enabled` setting.

    2. Phase 4 long-lived scheduler with banner/state machine:
       `schedule_update_check(tray_icon=..., event_callback=..., interval_hours=4.0)`.
       Returns the running `UpdateCheckScheduler`.

    The `min_supported` force-update path is in `schedule_min_supported_gate` and
    deliberately ignores the opt-out — that's the CVE response lever.
    """
    # Phase 1 shape: fire-and-forget tray notification thread.
    if delay_seconds is not None or repeat_interval_seconds is not None:
        if not is_update_notification_enabled(settings_manager):
            logger.debug("Update checker not scheduled; notifications disabled")
            return None
        url = _resolve_manifest_url(manifest_url)
        d = float(delay_seconds) if delay_seconds is not None else _CHECK_DELAY_SECONDS
        r = float(repeat_interval_seconds) if repeat_interval_seconds is not None else None

        def _delayed_check() -> None:
            _sleep_then_repeat(
                d,
                r,
                lambda: _do_one_notification_check(url, tray_icon, settings_manager),
            )

        thread = threading.Thread(target=_delayed_check, daemon=True, name="update-checker")
        thread.start()
        logger.debug("Update checker scheduled (delay=%ss, interval=%s)", d, r)
        return thread

    # Phase 4 shape: long-lived scheduler.
    url = manifest_url or _resolve_manifest_url(None)
    scheduler = UpdateCheckScheduler(
        tray_icon=tray_icon,
        manifest_url=url,
        event_callback=event_callback,
        initial_delay_seconds=(initial_delay_seconds if initial_delay_seconds is not None else _CHECK_DELAY_SECONDS),
        interval_seconds=max(60, int((interval_hours or 4.0) * 60 * 60)),
    )
    scheduler.start()
    return scheduler


def schedule_min_supported_gate(
    on_required_update: UpdateCallback,
    manifest_url: str | None = None,
    *,
    delay_seconds: float = _MIN_GATE_DELAY_SECONDS,
    repeat_interval_seconds: float | None = _CHECK_INTERVAL_SECONDS,
) -> threading.Thread:
    """Schedule the non-optional minimum-supported-version gate.

    This deliberately ignores VIOLA_DISABLE_UPDATE_CHECK and the SettingsManager
    opt-out because the min_supported field is the CVE response lever.
    """
    url = _resolve_manifest_url(manifest_url)

    def _delayed_gate() -> None:
        _sleep_then_repeat(
            delay_seconds,
            repeat_interval_seconds,
            lambda: _do_min_supported_gate(url, on_required_update),
        )

    thread = threading.Thread(target=_delayed_gate, daemon=True, name="min-supported-update-gate")
    thread.start()
    logger.debug(
        "Minimum-supported update gate scheduled (delay=%ss, interval=%s)",
        delay_seconds,
        repeat_interval_seconds,
    )
    return thread


__all__ = [
    "DEFAULT_VELOPACK_FEED_URL",
    "NATIVE_SELF_UPDATE_APPLY_SUPPORTED",
    "UPDATE_APPLY_BOUNDARY",
    "UpdateCheckScheduler",
    "UpdateStateStore",
    "cached_velopack_feed_url",
    "check_for_update",
    "is_update_notification_enabled",
    "reset_manifest_cache",
    "safe_update_download_url",
    "schedule_min_supported_gate",
    "schedule_update_check",
]
