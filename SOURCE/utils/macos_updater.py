"""macOS in-app update apply-leg (download, verify, swap) — fail-closed.

Windows applies updates through Velopack (``utils/velopack_updater.py``); Velopack
has no macOS runtime, so the Mac leg lives here. It takes a Mac artifact
descriptor selected by the platform-aware feed check
(``utils/update_checker.check_for_update`` → ``downloads.macos_arm64``) and:

1. downloads the notarized ``.app`` zip,
2. verifies the SHA-256 the manifest published (TLS-trusted-but-unsigned manifest,
   so the hash is the integrity anchor — same rule as the Windows leg),
3. verifies the code signature by **identity** — ``codesign --verify --deep
   --strict`` plus the Developer ID **Team ID** (``8GZQJ9R37R``), never a
   per-build thumbprint constant (the ``DEFAULT_SIGNING_THUMBPRINT`` anti-pattern
   #1545 warns against — a baked per-build value goes stale every release),
4. when the artifact is notarized, validates the stapled ticket
   (``stapler validate``) and Gatekeeper acceptance (``spctl --assess``); when it
   is not yet notarized (the founder credential gate is still open) it **skips
   the staple/Gatekeeper step with a loud note** rather than silently passing,
5. atomically swaps the running ``.app`` for the staged one.

Every method either succeeds or raises a structured error. Nothing on the verify
path swallows a failure and proceeds — a swallowed verification failure is exactly
how a tampered bundle would slip through. This is update plumbing: it does not
classify user queries or parse model output (no boxing of Viola's model).

``dry_run=True`` performs the full download → hash → extract → signature/identity
verify → locate-app → *decide the swap* pipeline but does not move the running
app into place. That is what makes the whole leg provable against a local feed
fixture without a notarized production artifact.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from updates.update_policy import ApplyDisposition, classify_apply

from core.logging_config import get_logger

logger = get_logger(__name__)

# Developer ID Application team for the Viola macOS signing identity
# (``Developer ID Application: JIHAD MOHAMMED SHKOUKANI (8GZQJ9R37R)``). This is
# an IDENTITY anchor, stable across releases — NOT a per-build hash. Overridable
# via env for a cert rotation without a code change.
DEFAULT_TEAM_ID = "8GZQJ9R37R"
_TEAM_ID_ENV = "VIOLA_MACOS_UPDATE_TEAM_ID"

# HTTP fetcher signature: (url) -> bytes. Injected in tests / fixtures.
HttpGetter = Callable[[str], bytes]
# Signature verifier signature: (app_path, expected_team_id) -> None (raises on fail).
SignatureVerifier = Callable[[Path, str], None]
# Notarization verifier signature: (app_path) -> None (raises on fail).
NotarizationVerifier = Callable[[Path], None]


class MacUpdateError(RuntimeError):
    """Base class for macOS update apply-leg failures."""


class MacUpdateSecurityError(MacUpdateError):
    """A downloaded artifact failed integrity or signature verification."""


class MacUpdateUnsupported(MacUpdateError):
    """The Mac apply leg cannot run here (e.g. invoked off macOS for a real swap)."""


@dataclass(frozen=True)
class MacUpdateArtifact:
    """A macOS update artifact descriptor, as published in ``downloads.macos_arm64``."""

    version: str
    url: str
    sha256: str
    filename: str = ""
    size_bytes: int = 0
    signing_team_id: str = ""
    signing_subject: str = ""
    notarized: bool = False

    @classmethod
    def from_manifest_entry(cls, entry: dict[str, object], *, version: str = "") -> MacUpdateArtifact:
        """Build an artifact from a manifest ``downloads.macos_arm64`` entry."""
        size = entry.get("size") or entry.get("size_bytes") or 0
        try:
            size_int = int(size)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            size_int = 0
        return cls(
            version=str(version or entry.get("version") or "").strip(),
            url=str(entry.get("url") or "").strip(),
            sha256=str(entry.get("sha256") or "").strip(),
            filename=str(entry.get("filename") or "").strip(),
            size_bytes=size_int,
            signing_team_id=str(entry.get("signing_team_id") or "").strip(),
            signing_subject=str(entry.get("signing_subject") or "").strip(),
            notarized=bool(entry.get("notarized") or entry.get("stapled") or False),
        )


@dataclass(frozen=True)
class MacUpdateResult:
    """Outcome of a download+verify (+optional swap) run."""

    status: str  # "staged" | "applied" | "dry_run"
    version: str
    staged_app_path: str
    swap_target_path: str
    notarization_verified: bool
    detail: str = ""


def _expected_team_id(override: str | None = None) -> str:
    if override:
        return override.strip()
    env_value = os.environ.get(_TEAM_ID_ENV, "").strip()
    return env_value or DEFAULT_TEAM_ID


def _default_http_get(url: str) -> bytes:
    """Download bytes over https, or read a local ``file://``/path fixture.

    Local paths are permitted ONLY so a dry-run can target a local feed fixture;
    a real remote URL must be https (the manifest is TLS-trusted).
    """
    parsed = urlparse(url)
    if parsed.scheme in ("", "file"):
        local = Path(parsed.path if parsed.scheme == "file" else url)
        return local.read_bytes()
    if parsed.scheme != "https":
        raise MacUpdateSecurityError("macOS update download requires an https URL, got scheme %r" % parsed.scheme)
    import httpx

    resp = httpx.get(url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().lower()


def _verify_sha256(payload: bytes, expected: str) -> None:
    want = str(expected or "").strip().lower()
    if not want:
        raise MacUpdateSecurityError("Expected SHA-256 is missing from the manifest artifact descriptor")
    got = sha256_bytes(payload)
    if got != want:
        raise MacUpdateSecurityError("Artifact SHA-256 mismatch: expected=%s actual=%s" % (want[:16], got[:16]))


def _extract_app(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the zip and return the contained ``.app`` bundle path.

    On macOS uses ``ditto -x -k`` so the code signature's symlinks and extended
    attributes survive (a plain unzip corrupts framework signatures). Off macOS
    (CI / fixture dry-run) falls back to ``zipfile`` with a note.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin" and shutil.which("ditto"):
        subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(dest_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300.0,
        )
    else:
        logger.info(
            "ditto unavailable (%s); extracting the update zip with zipfile — "
            "fine for a fixture dry-run, but a real notarized bundle must be extracted with ditto on macOS.",
            sys.platform,
        )
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest_dir)
    apps = sorted(p for p in dest_dir.rglob("*.app") if p.is_dir())
    if not apps:
        raise MacUpdateError("No .app bundle found in the downloaded update archive")
    # Shallowest .app wins (the top-level bundle, not a nested helper .app).
    apps.sort(key=lambda p: len(p.parts))
    return apps[0]


def _read_team_id(app_path: Path) -> str:
    """Read the signing Team ID from ``codesign -dvvv`` output."""
    completed = subprocess.run(
        ["codesign", "-dvvv", str(app_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    # codesign writes the signature details to stderr.
    for line in (completed.stderr or "").splitlines():
        if line.startswith("TeamIdentifier="):
            return line.split("=", 1)[1].strip()
    return ""


def _macos_signature_verifier(app_path: Path, expected_team_id: str) -> None:
    """Real macOS identity verification: codesign strict + Team ID match.

    Raises ``MacUpdateSecurityError`` on any failure. Gatekeeper/staple are
    verified separately (they only hold once the artifact is notarized).
    """
    if sys.platform != "darwin":
        raise MacUpdateUnsupported("codesign signature verification requires macOS")
    try:
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120.0,
        )
    except subprocess.CalledProcessError as exc:
        raise MacUpdateSecurityError("codesign --verify failed: %s" % (exc.stderr or exc.stdout or exc)) from exc
    actual_team = _read_team_id(app_path)
    if actual_team != expected_team_id:
        raise MacUpdateSecurityError(
            "Update bundle Team ID mismatch: expected=%s actual=%s" % (expected_team_id, actual_team or "missing")
        )


def _verify_notarization(app_path: Path) -> None:
    """Validate the stapled notarization ticket and Gatekeeper acceptance.

    Only meaningful once the artifact has been notarized + stapled. Raises on
    failure so an artifact that CLAIMS to be notarized but is not fails closed.
    """
    if sys.platform != "darwin":
        raise MacUpdateUnsupported("stapler/spctl validation requires macOS")
    try:
        subprocess.run(
            ["xcrun", "stapler", "validate", str(app_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
        subprocess.run(
            ["spctl", "--assess", "--type", "exec", "--verbose=4", str(app_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except subprocess.CalledProcessError as exc:
        raise MacUpdateSecurityError(
            "Notarization validation failed (stapler/spctl): %s" % (exc.stderr or exc.stdout or exc)
        ) from exc


def download_and_stage(
    artifact: MacUpdateArtifact,
    work_dir: str | Path,
    *,
    expected_team_id: str | None = None,
    require_notarization: bool | None = None,
    http_get: HttpGetter | None = None,
    signature_verifier: SignatureVerifier | None = None,
    notarization_verifier: NotarizationVerifier | None = None,
) -> Path:
    """Download, verify, and extract ``artifact`` — return the staged ``.app`` path.

    Verification order (each fails closed):
      SHA-256 → extract → codesign strict + Team ID identity → (notarization when
      required / claimed, else a loud skip note).

    ``require_notarization`` defaults to ``artifact.notarized``: an artifact that
    advertises itself as notarized MUST pass staple/Gatekeeper validation; one
    that does not (the credential gate is still open) skips it with a loud note.
    Pass ``require_notarization=True`` to hard-require it regardless.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    team_id = _expected_team_id(expected_team_id)
    getter = http_get or _default_http_get
    verifier = signature_verifier or _macos_signature_verifier
    notary_verifier = notarization_verifier or _verify_notarization
    want_notarization = artifact.notarized if require_notarization is None else bool(require_notarization)

    if not artifact.url:
        raise MacUpdateError("Artifact descriptor has no url — nothing to download")

    logger.info("Downloading macOS update %s from %s", artifact.version or "?", artifact.url)
    payload = getter(artifact.url)
    _verify_sha256(payload, artifact.sha256)

    zip_path = work / (artifact.filename or "Viola-macos-update.zip")
    zip_path.write_bytes(payload)

    staged_app = _extract_app(zip_path, work / "extracted")

    # Identity-based signature verification (never a per-build thumbprint).
    verifier(staged_app, team_id)

    if want_notarization:
        notary_verifier(staged_app)
        logger.info("macOS update %s passed notarization + Gatekeeper validation", artifact.version or "?")
    else:
        logger.warning(
            "macOS update %s is NOT marked notarized — skipping stapler/Gatekeeper validation with this loud note. "
            "codesign identity (Team %s) was still verified. Publish a notarized artifact to enable full validation.",
            artifact.version or "?",
            team_id,
        )
    return staged_app


def apply_staged_update(
    staged_app: str | Path,
    current_app_path: str | Path,
    *,
    dry_run: bool = False,
) -> Path:
    """Atomically swap ``current_app_path`` for ``staged_app``; return the new path.

    ``dry_run=True`` returns the path that WOULD be swapped without moving
    anything — this is what makes the leg provable against a fixture without
    disturbing a running install. A real swap requires macOS.
    """
    staged = Path(staged_app)
    current = Path(current_app_path)
    if not staged.exists():
        raise MacUpdateError("Staged app does not exist: %s" % staged)

    if dry_run:
        logger.info("[dry-run] would swap %s <- %s (not moving)", current, staged)
        return current

    if sys.platform != "darwin":
        raise MacUpdateUnsupported("Applying a macOS update (bundle swap) requires macOS")

    backup = current.with_suffix(current.suffix + ".bak")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if current.exists():
        current.rename(backup)
    try:
        shutil.move(str(staged), str(current))
    except Exception:
        # Roll back to the backup so we never leave the user without an app.
        if backup.exists() and not current.exists():
            backup.rename(current)
        raise
    logger.info("macOS update applied: %s", current)
    return current


def check_download_verify(
    artifact: MacUpdateArtifact,
    current_app_path: str | Path,
    *,
    work_dir: str | Path | None = None,
    dry_run: bool = True,
    expected_team_id: str | None = None,
    require_notarization: bool | None = None,
    http_get: HttpGetter | None = None,
    signature_verifier: SignatureVerifier | None = None,
    notarization_verifier: NotarizationVerifier | None = None,
) -> MacUpdateResult:
    """High-level Mac apply leg: download+verify, then swap (unless ``dry_run``).

    Defaults to ``dry_run=True`` — the safe, fixture-provable mode. Production
    apply passes ``dry_run=False`` (macOS only) once a notarized artifact exists.
    """
    owns_work_dir = work_dir is None
    work = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="viola-macupdate-"))
    try:
        staged_app = download_and_stage(
            artifact,
            work,
            expected_team_id=expected_team_id,
            require_notarization=require_notarization,
            http_get=http_get,
            signature_verifier=signature_verifier,
            notarization_verifier=notarization_verifier,
        )
        swap_target = apply_staged_update(staged_app, current_app_path, dry_run=dry_run)
        notarized_ok = artifact.notarized if require_notarization is None else bool(require_notarization)
        return MacUpdateResult(
            status="dry_run" if dry_run else "applied",
            version=artifact.version,
            staged_app_path=str(staged_app),
            swap_target_path=str(swap_target),
            notarization_verified=bool(notarized_ok),
            detail="verified sha256 + codesign identity" + (" + notarization" if notarized_ok else " (staple skipped)"),
        )
    finally:
        # In dry-run we keep the staged app so the caller can inspect it; only
        # clean the temp dir we created on a real apply (staged app moved out).
        if owns_work_dir and not dry_run:
            shutil.rmtree(work, ignore_errors=True)


def read_bundle_version(app_path: str | Path) -> str:
    """Best-effort read of ``CFBundleShortVersionString`` from a bundle Info.plist."""
    plist = Path(app_path) / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
        return str(data.get("CFBundleShortVersionString") or data.get("CFBundleVersion") or "").strip()
    except (OSError, plistlib.InvalidFileException):
        return ""


# ---------------------------------------------------------------------------
# In-app apply adapter (#2634) — wire the leg above into the running app.
#
# Everything above is the pure apply *leg* (download/verify/swap). Nothing in
# the shipping app invoked it, so on macOS the running Viola never actually
# applied an update — the apply path existed only in tests (checklist step 4
# over-claimed it "swaps the app"). ``MacUpdater`` is the missing adapter: it
# presents the SAME method the update banner / webview window already call for
# the Windows Velopack path — ``apply_and_restart(manifest, user_consent=...)`` —
# so the existing offered/forced consent policy, the banner, and the
# manual-reinstall fallback all drive it with ZERO UI changes. On macOS,
# ``viola_qt._attach_update_scheduler`` constructs this instead of a
# ``VelopackUpdater`` (Velopack has no macOS runtime).
# ---------------------------------------------------------------------------

# Relauncher signature: (new_app_path) -> None. Injected in tests / fixtures.
Relauncher = Callable[[Path], None]
# Apply-leg signature, matching ``check_download_verify``. Injected in tests so the
# adapter's policy/relaunch orchestration is provable without macOS-only tooling.
ApplyFn = Callable[..., MacUpdateResult]


class MacUpdateConsentRequired(MacUpdateError):
    """apply_and_restart was called for an OFFERED update without user consent.

    Mirrors Velopack's ``ConsentRequiredError``: a routine (non-mandatory)
    update may be downloaded and verified, but applying it requires the user to
    have accepted. Reaching apply without consent for an OFFERED update is a
    policy violation — we refuse rather than relaunch onto an unaccepted version.
    """


def resolve_current_app_bundle(executable: str | Path | None = None) -> Path:
    """Locate the running ``.app`` bundle from ``sys.executable``, or raise.

    A shipped Mac install runs from ``.../Viola.app/Contents/MacOS/Viola``; the
    swap target is the enclosing ``.app``. When no ``.app`` ancestor exists (a
    dev ``python viola_qt.py`` run), there is nothing to swap — we raise
    ``MacUpdateUnsupported`` so the caller keeps the manual path instead of
    swapping an arbitrary directory. Fail closed, never guess a target.
    """
    exe = Path(executable or sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    raise MacUpdateUnsupported(
        "Could not locate the running .app bundle from %s; macOS in-app apply needs an installed .app layout" % exe
    )


def artifact_from_manifest(manifest: dict[str, object]) -> MacUpdateArtifact:
    """Build a :class:`MacUpdateArtifact` from a ``check_for_update`` result dict.

    Prefers the platform-projected ``platform_artifact`` entry (the manifest's
    ``downloads.<platform_key>`` block — the only place ``signing_team_id`` and
    ``notarized`` live) and pins the version to the manifest's advertised
    ``latest_version`` (the qualified candidate the user was offered), mirroring
    the Velopack version pin. Falls back to the top-level ``url``/``sha256``.

    The artifact URL must be https (defense in depth — the manifest is
    TLS-trusted but unsigned; ``_default_http_get`` enforces the same rule at
    download time). A missing URL is a hard error, not a silent no-op.
    """
    entry = manifest.get("platform_artifact")
    entry = dict(entry) if isinstance(entry, dict) else {}
    version = str(manifest.get("latest_version") or entry.get("version") or "").strip()
    url = str(entry.get("url") or manifest.get("url") or "").strip()
    sha256 = str(entry.get("sha256") or manifest.get("sha256") or "").strip()
    if not url:
        raise MacUpdateError("Update manifest carries no macOS artifact url to apply")
    scheme = urlparse(url).scheme
    if scheme != "https":
        raise MacUpdateSecurityError("macOS artifact url must be https, got scheme %r" % scheme)
    merged = dict(entry)
    merged["url"] = url
    merged["sha256"] = sha256
    return MacUpdateArtifact.from_manifest_entry(merged, version=version)


def _default_relaunch(new_app_path: Path) -> None:
    """Relaunch into the swapped bundle once this process exits (macOS only).

    Spawns a detached helper that waits for THIS pid to exit, then ``open``s the
    new bundle. Waiting for exit first matters: ``open`` on a still-running app
    just reactivates the old instance, so the fresh version would never take
    over. Detached (``start_new_session``) so it survives our own termination.
    """
    if sys.platform != "darwin":
        raise MacUpdateUnsupported("macOS relaunch requires macOS")
    pid = os.getpid()
    script = "while /bin/kill -0 %d 2>/dev/null; do /bin/sleep 0.2; done; exec /usr/bin/open -n %s" % (
        pid,
        shlex.quote(str(new_app_path)),
    )
    subprocess.Popen(
        ["/bin/sh", "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("Scheduled macOS relaunch of %s after pid %d exits", new_app_path, pid)


def _default_quit() -> None:
    """Ask the running Qt app to quit; if there is none, exit for the relaunch."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.quit()
            return
    except Exception:  # noqa: BLE001, RUF100 - Qt/PySide import or teardown can raise anything (missing
        # module, deleted-wrapper RuntimeError, etc.); os._exit(0) below is the safe fallback either way.
        logger.debug("Qt unavailable for graceful quit; exiting process so the relaunch can take over")
    os._exit(0)


class MacUpdater:
    """macOS native-apply adapter mirroring ``VelopackUpdater``'s apply contract.

    The banner / window only ever call one method on the injected updater —
    ``apply_and_restart(manifest, *, user_consent=...)`` — so implementing just
    that here drops the whole macOS apply path into the existing update UI with
    no banner changes. It reuses the shared ``updates.update_policy`` decision
    (OFFERED needs consent, FORCED auto-applies for the closed safety/brick
    class) and the verified apply leg above.

    Fail-closed: ``apply_and_restart`` either verifies + swaps + relaunches, or
    raises. A verify failure never swaps (the banner routes a raise to the
    manual-reinstall fallback); a successful swap whose relaunch cannot be
    spawned is reported honestly as "applied, takes effect next launch" rather
    than as a failure the user would (wrongly) re-download to fix. It never
    claims a false success. This is update plumbing — no boxing of Viola's model.
    """

    def __init__(
        self,
        *,
        current_app_path: str | Path | None = None,
        expected_team_id: str | None = None,
        require_notarization: bool | None = None,
        http_get: HttpGetter | None = None,
        signature_verifier: SignatureVerifier | None = None,
        notarization_verifier: NotarizationVerifier | None = None,
        relauncher: Relauncher | None = None,
        quitter: Callable[[], None] | None = None,
        apply_fn: ApplyFn | None = None,
    ) -> None:
        self._current_app_path = Path(current_app_path) if current_app_path else None
        self._expected_team_id = expected_team_id
        self._require_notarization = require_notarization
        self._http_get = http_get
        self._signature_verifier = signature_verifier
        self._notarization_verifier = notarization_verifier
        self._relauncher = relauncher or _default_relaunch
        self._quitter = quitter or _default_quit
        self._apply_fn = apply_fn or check_download_verify

    def _current_app(self) -> Path:
        return self._current_app_path or resolve_current_app_bundle()

    def is_supported(self) -> bool:
        """True only on macOS with a resolvable installed ``.app`` layout."""
        if sys.platform != "darwin":
            return False
        try:
            self._current_app()
            return True
        except MacUpdateError:
            return False

    def apply_and_restart(self, manifest: dict[str, object], *, user_consent: bool = False) -> MacUpdateResult:
        """Verify + swap + relaunch the running app onto the manifest's version.

        Consent-gated by ``updates.update_policy.classify_apply``:

        - NONE (nothing available) -> ``MacUpdateError``.
        - OFFERED (routine) -> apply ONLY when ``user_consent`` is True; else
          ``MacUpdateConsentRequired`` (never relaunch onto an unaccepted version).
        - FORCED (closed safety/brick class) -> apply without requiring consent.

        The verify + swap is the leg above (``check_download_verify`` with
        ``dry_run=False``), which raises on any integrity / signature /
        notarization failure and never swaps a bad bundle. Only after a
        successful swap do we schedule the relaunch and quit.
        """
        decision = classify_apply(manifest)
        if decision.disposition is ApplyDisposition.NONE:
            raise MacUpdateError("apply_and_restart called with no available update")
        if decision.disposition is ApplyDisposition.OFFERED and not user_consent:
            raise MacUpdateConsentRequired(
                "Refusing to apply an offered (non-mandatory) macOS update without explicit user consent"
            )

        current_app = self._current_app()
        artifact = artifact_from_manifest(manifest)
        result = self._apply_fn(
            artifact,
            current_app,
            dry_run=False,
            expected_team_id=self._expected_team_id,
            require_notarization=self._require_notarization,
            http_get=self._http_get,
            signature_verifier=self._signature_verifier,
            notarization_verifier=self._notarization_verifier,
        )

        # The swap has happened (check_download_verify raises on any failure and
        # never swaps a bad bundle). Schedule the relaunch; only quit if it was
        # actually spawned.
        new_app = Path(result.swap_target_path)
        try:
            self._relauncher(new_app)
        except Exception as exc:  # noqa: BLE001, RUF100 - relauncher is an injectable callable (Relauncher
            # protocol) that can raise anything from a real subprocess spawn; the swap already succeeded,
            # so any failure here is reported honestly below rather than crashing an already-applied update.
            # Honest failure state: the on-disk bundle IS the new version now, so
            # this is NOT an apply failure — reporting it as one would send the
            # user to re-download an already-applied update. The new version runs
            # on the next manual launch.
            logger.warning(
                "macOS update %s applied on disk but the automatic relaunch could not be started (%s); "
                "the new version will run on the next launch.",
                artifact.version or "?",
                exc,
            )
            return result

        self._quitter()
        return result


__all__ = [
    "DEFAULT_TEAM_ID",
    "ApplyFn",
    "MacUpdateArtifact",
    "MacUpdateConsentRequired",
    "MacUpdateError",
    "MacUpdateResult",
    "MacUpdateSecurityError",
    "MacUpdateUnsupported",
    "MacUpdater",
    "Relauncher",
    "apply_staged_update",
    "artifact_from_manifest",
    "check_download_verify",
    "download_and_stage",
    "read_bundle_version",
    "resolve_current_app_bundle",
    "sha256_bytes",
]
