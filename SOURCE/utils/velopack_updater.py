"""Velopack native-apply client adapter (download + verify + apply, fail-closed).

This wraps the Velopack Python runtime (``velopack.UpdateManager`` /
``velopack.App``) so the desktop app can detect, signature-verify, and apply an
update in place — moving a working install to the next version without a manual
re-download.

Three things make this safety-critical and shape the whole design:

1. **It only runs on a real Velopack-installed layout.** Velopack's
   ``UpdateManager`` reads the ``current/sq.version`` app manifest that Velopack's
   own ``Setup.exe`` / ``.msi`` writes under ``%LocalAppData%\\{packId}``. Our
   shipping installer is Inno Setup (``viola_installer.iss``), which installs a
   flat tree into Program Files with no ``current/`` folder, no ``sq.version``,
   and no ``Update.exe``. On such a layout ``UpdateManager`` raises
   ``NotInstalled`` ("Could not auto-locate app manifest"). This adapter does NOT
   paper over that — it surfaces it as a clean ``VelopackNotInstalled`` so the
   caller falls back to the manual-reinstall path instead of silently
   half-applying. (Whether we switch the desktop installer to Velopack's own
   Setup.exe is an OPEN founder decision — see the W1 report. Until then this
   path is import-gated by ``NATIVE_SELF_UPDATE_APPLY_SUPPORTED`` and is inert in
   production.)

2. **Offered-by-default, forced-by-exception (LOCKED policy).** A routine update
   is downloaded and verified but applied ONLY on explicit user consent. The
   adapter therefore separates ``check_and_stage()`` (download + verify, no
   apply) from ``apply_and_restart()`` (the one method that relaunches). The
   forced exception is gated through ``updates.update_policy.classify_apply``:
   the adapter NEVER auto-applies a non-mandatory update — only a release the
   policy resolves to FORCED (closed safety/brick class) may apply without a
   consent token.

3. **Fail closed on the apply path.** Every method either succeeds or raises /
   returns a structured failure. Nothing on the apply path swallows an error and
   continues — a swallowed verification failure is exactly how a tampered binary
   would slip through.

The adapter does not classify user queries or parse model output — it is update
plumbing (no boxing of Viola's model).
"""

from __future__ import annotations

import datetime
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from updates.update_policy import ApplyDisposition, classify_apply

from core.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = get_logger(__name__)

# Stable channel maps to Velopack's "win" channel; everything else passes through
# lower-cased (matches scripts/build_velopack_release.ps1's $vpkChannel rule).
_STABLE_VELOPACK_CHANNEL = "win"


class VelopackError(RuntimeError):
    """Base class for Velopack adapter failures."""


class VelopackUnavailable(VelopackError):
    """The velopack runtime could not be imported (not a packaged build)."""


class VelopackNotInstalled(VelopackError):
    """The running app is not a Velopack-installed layout (e.g. Inno Setup).

    This is the expected, fail-closed signal that the native-apply path cannot
    run here and the caller must fall back to the manual-reinstall path.
    """


class VelopackApplyError(VelopackError):
    """An update was available but could not be downloaded/verified/applied."""


class ConsentRequiredError(VelopackError):
    """apply_and_restart was called for an OFFERED update without a consent token.

    The whole point of offered-by-default: a routine update may be downloaded and
    verified, but applying it requires the user to have accepted. A caller that
    reaches apply without consent for a non-forced update is a policy violation,
    and we refuse rather than silently relaunch onto a version the user did not
    accept.
    """


@dataclass(frozen=True)
class StageResult:
    """Outcome of a check + download (no apply performed)."""

    status: str  # "staged" | "noop" | "downgrade_blocked" | "failed"
    detail: str
    version: str = ""
    disposition: str = ApplyDisposition.NONE.value
    may_auto_apply: bool = False


def velopack_channel(channel: str | None) -> str:
    normalized = str(channel or "stable").strip().lower()
    return _STABLE_VELOPACK_CHANNEL if normalized in ("", "stable", "win") else normalized


def _require_https_feed(feed_url: object) -> str:
    """Return ``feed_url`` as a string, or raise if it is not an https:// URL.

    The feed URL is where signed binaries come from -- never accept a non-https
    feed (a http feed is a trivial MITM apply vector).
    """
    if not feed_url or not str(feed_url).lower().startswith("https://"):
        raise VelopackApplyError("Velopack feed URL must be https://")
    return str(feed_url)


def _import_velopack() -> Any:
    try:
        import velopack  # type: ignore
    except Exception as exc:  # ImportError or a load error from the .pyd
        raise VelopackUnavailable("velopack runtime is not importable: %s" % exc) from exc
    return velopack


def _is_not_installed_error(exc: Exception) -> bool:
    """Heuristic: did UpdateManager fail because this is not a Velopack install?

    Velopack raises a NotInstalledException / a message about not being able to
    auto-locate the app manifest when run outside an installed layout. We match
    on the type name and the message rather than importing a specific exception
    class (the Python binding's exception surface is thin and version-dependent),
    and we bias toward treating an ambiguous failure as "not installed" ONLY for
    construction — never for an apply (an apply failure always raises).
    """
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "notinstalled" in name
        or "not properly installed" in message
        or "could not auto-locate" in message
        or "app manifest" in message
    )


class VelopackUpdater:
    """Adapter over ``velopack.UpdateManager`` with offered/forced apply policy."""

    def __init__(
        self,
        feed_url: str | Callable[[], str],
        *,
        channel: str | None = None,
        allow_downgrade: bool = False,
        manager_factory: Callable[..., Any] | None = None,
    ) -> None:
        """``feed_url`` may be a string or a zero-arg callable resolved on first use.

        The callable form exists so the app can construct this adapter at startup
        without paying for the feed URL up front. The URL lives in the update
        manifest, and reading it eagerly meant a synchronous network fetch on the
        main thread before the window was ever shown. Nothing here touches the
        network until ``_build_manager`` runs (the user applies an update), so the
        lookup belongs there, not in the constructor. A literal string is still
        validated eagerly, so ``VelopackUpdater("http://...")`` raises exactly as
        it always did.
        """
        self._feed_url_source = feed_url
        if not callable(feed_url):
            _require_https_feed(feed_url)
        self.channel = velopack_channel(channel)
        self.allow_downgrade = bool(allow_downgrade)
        self._manager_factory = manager_factory
        self._manager: Any | None = None
        self._velopack: Any | None = None

    @property
    def feed_url(self) -> str:
        """The resolved, https-validated feed URL (resolves the callable form)."""
        source = self._feed_url_source
        return _require_https_feed(source() if callable(source) else source)

    # -- construction -----------------------------------------------------

    def _build_manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        if self._manager_factory is not None:
            # The factory path must honor the same fail-closed contract as the
            # real construction below: a not-installed signal becomes
            # VelopackNotInstalled, any other failure becomes VelopackApplyError.
            # Otherwise an injected/real factory could leak a raw exception past
            # is_installed()/check_and_stage() and skip the manual-reinstall
            # fallback.
            try:
                self._manager = self._manager_factory(self.feed_url)
            except VelopackError:
                raise
            except Exception as exc:
                if _is_not_installed_error(exc):
                    raise VelopackNotInstalled(
                        "Not a Velopack-installed layout; native apply unavailable: %s" % exc
                    ) from exc
                raise VelopackApplyError("Could not construct Velopack UpdateManager: %s" % exc) from exc
            return self._manager
        velopack = _import_velopack()
        self._velopack = velopack
        options_cls = getattr(velopack, "UpdateOptions", None)
        try:
            if options_cls is not None:
                opts = options_cls()
                # Default-deny downgrade: the offered/forced policy never wants a
                # silent downgrade, and AllowVersionDowngrade=False is the
                # Velopack-level enforcement of "downgrade blocked". If we cannot
                # set this gate (a renamed/removed attribute in some velopack
                # build), we must NOT construct a manager that silently uses a
                # permissive default — fail closed rather than risk a downgrade.
                try:
                    opts.AllowVersionDowngrade = self.allow_downgrade
                    opts.ExplicitChannel = self.channel
                except AttributeError as exc:
                    raise VelopackApplyError(
                        "Could not set UpdateOptions safety gates " "(AllowVersionDowngrade/ExplicitChannel): %s" % exc
                    ) from exc
                self._manager = velopack.UpdateManager(self.feed_url, opts)
            else:
                self._manager = velopack.UpdateManager(self.feed_url)
        except Exception as exc:
            if _is_not_installed_error(exc):
                raise VelopackNotInstalled(
                    "Not a Velopack-installed layout; native apply unavailable: %s" % exc
                ) from exc
            raise VelopackApplyError("Could not construct Velopack UpdateManager: %s" % exc) from exc
        return self._manager

    def is_installed(self) -> bool:
        """True only when the running app is a real Velopack-installed layout."""
        try:
            self._build_manager()
            return True
        except VelopackNotInstalled:
            return False
        except VelopackError:
            return False

    # -- check + download (NO apply) -------------------------------------

    def check_and_stage(self) -> StageResult:
        """Check the feed and download the update — but do NOT apply.

        Returns a structured result. Applying is a separate, explicit step
        (apply_and_restart) so the offered-by-default policy holds: nothing here
        relaunches the app. Velopack verifies the downloaded package's signature
        internally before staging; a failure raises and we surface it as a failed
        result rather than proceeding.
        """
        manager = self._build_manager()
        try:
            info = manager.check_for_updates()
        except Exception as exc:
            raise VelopackApplyError("check_for_updates failed: %s" % exc) from exc

        if info is None:
            return StageResult(status="noop", detail="up_to_date")

        if self._is_downgrade(info) and not self.allow_downgrade:
            # Defense in depth beyond UpdateOptions.AllowVersionDowngrade=False.
            return StageResult(
                status="downgrade_blocked",
                detail="downgrade_blocked",
                version=self._info_version(info),
            )

        try:
            manager.download_updates(info)
        except Exception as exc:
            # Velopack raises here on signature / integrity verification failure.
            # We do NOT continue to apply — fail closed.
            raise VelopackApplyError("download_updates failed (verification or network): %s" % exc) from exc

        return StageResult(
            status="staged",
            detail="downloaded_and_verified",
            version=self._info_version(info),
        )

    # -- apply (the only relaunch path) ----------------------------------

    def apply_and_restart(
        self,
        manifest: dict[str, object],
        *,
        user_consent: bool = False,
    ) -> None:
        """Apply a staged update and relaunch — consent-gated by policy.

        ``manifest`` is the update-check result (it carries ``available`` and the
        ``mandatory`` signal). The disposition is resolved by
        ``updates.update_policy.classify_apply``:

        - FORCED (closed safety/brick class): apply without requiring consent.
        - OFFERED (everything else): apply ONLY when ``user_consent`` is True;
          otherwise raise ``ConsentRequiredError`` (we never relaunch onto a
          version the user did not accept).
        - NONE: nothing to apply -> raise ``VelopackApplyError``.

        This is the structural guarantee that a non-mandatory update never
        auto-applies: the only way to reach the relaunch for an OFFERED update is
        an explicit ``user_consent=True`` from the accept UI.
        """
        decision = classify_apply(manifest)
        if decision.disposition is ApplyDisposition.NONE:
            raise VelopackApplyError("apply_and_restart called with no available update")
        if decision.disposition is ApplyDisposition.OFFERED and not user_consent:
            raise ConsentRequiredError(
                "Refusing to apply an offered (non-mandatory) update without explicit user consent"
            )

        manager = self._build_manager()
        # Probe the already-staged update first; this is a best-effort lookup
        # whose failure is NOT fatal because the check_for_updates fallback below
        # IS fail-closed (it raises on failure). A None here just means "fall back
        # to re-resolving"; the apply itself (apply_updates_and_restart) is the
        # gate that must never be reached without a real, verified update.
        info = None
        try:
            info = manager.get_update_pending_restart()
        except Exception:
            info = None
        if info is None:
            try:
                info = manager.check_for_updates()
            except Exception as exc:
                raise VelopackApplyError("could not resolve pending update to apply: %s" % exc) from exc
        if info is None:
            raise VelopackApplyError("no staged update to apply")

        # Version pin — the apply target MUST be the version the manifest
        # advertised (the qualified candidate the user consented to). Velopack
        # resolves "newest in the feed", which can be AHEAD of the manifest
        # (e.g. a feed uploaded for a release that never passed qualification
        # or never flipped). Applying feed-newest would bypass both the
        # qualification gate and the cohort rollout, so a mismatch is a hard
        # refusal, and a manifest without a pinnable version is equally
        # refused (fail closed).
        expected_version = str(manifest.get("latest_version") or "").strip()
        if not expected_version:
            raise VelopackApplyError("manifest carries no latest_version to pin the apply to; refusing to apply")
        resolved_version = self._info_version(info).strip()
        if resolved_version != expected_version:
            raise VelopackApplyError(
                "version pin mismatch: manifest advertises %s but the feed resolved %s; "
                "refusing to apply an unadvertised version" % (expected_version, resolved_version)
            )

        try:
            manager.apply_updates_and_restart(info)
        except Exception as exc:
            raise VelopackApplyError("apply_updates_and_restart failed: %s" % exc) from exc

    def get_pending_restart(self) -> Any | None:
        """Read-only probe: is a verified update already staged for restart?

        This never applies anything — it only reports whether Velopack already
        has a pending update. A failure here means "cannot determine", which is
        safely None (no pending update surfaced); it does not gate the apply.
        """
        manager = self._build_manager()
        try:
            return manager.get_update_pending_restart()
        except Exception:
            return None

    # -- helpers ----------------------------------------------------------

    def _is_downgrade(self, info: Any) -> bool:
        for attr in ("IsDowngrade", "is_downgrade"):
            value = getattr(info, attr, None)
            if isinstance(value, bool):
                return value
        return False

    def _info_version(self, info: Any) -> str:
        target = getattr(info, "TargetFullRelease", None) or getattr(info, "target_full_release", None)
        if target is not None:
            version = getattr(target, "Version", None) or getattr(target, "version", None)
            if version:
                return str(version)
        version = getattr(info, "Version", None) or getattr(info, "version", None)
        return str(version) if version else ""


def install_startup_hook(auto_apply_on_startup: bool = False) -> bool:
    """Run Velopack's startup hook (``App.run()``) before Qt is constructed.

    Velopack requires ``App.run()`` to be called at the very top of the process,
    before any UI infrastructure, so it can handle the install/uninstall/updated
    command-line arguments injected by its installer. On a non-Velopack (dev or
    Inno) build this raises internally; that is expected and we fail OPEN at
    startup (returning False) — fail-open on the *startup hook* is correct
    because the hook is about handling installer args, not about applying an
    update. The apply path itself remains fail-closed.

    ``auto_apply_on_startup`` defaults False: the offered-by-default policy drives
    apply explicitly, it never lets Velopack silently apply a pending update on
    the next launch.
    """
    try:
        velopack = _import_velopack()
    except VelopackUnavailable:
        logger.debug("Velopack startup hook skipped: runtime unavailable (dev/non-packaged run)")
        return False
    try:
        app = velopack.App()
        try:
            app.set_auto_apply_on_startup(bool(auto_apply_on_startup))
        except Exception:
            logger.debug("Velopack set_auto_apply_on_startup unavailable; continuing")
        app.run()
        return True
    except BaseException as exc:  # noqa: BLE001, RUF100 - see below; must fail open, never kill the launch
        # velopack's native App.run() binding does not always signal "not a
        # Velopack-installed layout" as a catchable Exception subclass -- on at
        # least one platform binding it raises/propagates SystemExit (a
        # BaseException, deliberately NOT caught by `except Exception:`). This
        # function's own docstring already promises fail-open ("that is
        # expected and we fail OPEN at startup"); `except Exception:` silently
        # violated that promise whenever the failure surfaced as SystemExit,
        # which would kill the app before Qt ever starts. Confirmed live on the
        # sibling inline hook in viola_qt.py (main run 29332068457/29336136287,
        # #1518) -- same velopack.App().run() call, same failure shape.
        logger.debug("Velopack startup hook inactive (not a Velopack install): %s: %s", type(exc).__name__, exc)
        return False


# ---------------------------------------------------------------------------
# Settings > Apps entry self-heal
# ---------------------------------------------------------------------------
#
# Velopack's Setup.exe writes the per-user uninstall key
# HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\<packId> during
# install, Update.exe rewrites it after every applied update, and only
# ``Update.exe --uninstall`` (or an app-id change during apply) deletes it.
# A 1.0.5 laptop install still ended up with no key and therefore no
# Settings > Apps entry, after an earlier cleanup script and a previous
# uninstall had both been through that key. Without the entry the user has no
# supported way to remove Viola, so the app repairs it: when it is running from
# a Velopack install root and the key is missing, it writes the same values
# Velopack writes (value names and formats from vpk 0.0.1589's
# src/bins/src/windows/registry.rs, checked against the clean-VM receipt).

_UNINSTALL_REGISTRY_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
_UNINSTALL_SELF_HEAL_LOCK = threading.Lock()
_UNINSTALL_SELF_HEAL_STARTED = False


@dataclass(frozen=True)
class VelopackInstallLayout:
    """The pieces of a Velopack install root the uninstall entry points at."""

    root_dir: str
    update_exe: str
    main_exe: str
    pack_id: str
    title: str
    authors: str
    version: str


def _read_sq_version(manifest_path: Path) -> dict[str, str]:
    import xml.etree.ElementTree as ET  # nosec B405 - Velopack's own manifest in our install root

    tree = ET.parse(str(manifest_path))  # nosec B314 - written by Velopack Setup, not network input
    values: dict[str, str] = {}
    for element in tree.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"id", "title", "authors", "version", "mainExe"} and element.text:
            values[tag] = element.text.strip()
    return values


def detect_velopack_install_layout(executable: str | None = None) -> VelopackInstallLayout | None:
    """Return the Velopack install root this process runs from, or None.

    A Velopack install is ``<root>/Update.exe`` plus ``<root>/current/`` holding
    the app and its ``sq.version`` manifest. Anything else (dev run, Inno
    install, portable build) is not ours to register.
    """
    exe = Path(executable or sys.executable)
    current_dir = exe.parent
    if current_dir.name.lower() != "current":
        return None
    root = current_dir.parent
    update_exe = root / "Update.exe"
    manifest = current_dir / "sq.version"
    if not update_exe.is_file() or not manifest.is_file():
        return None
    if (root / ".portable").exists():
        # Velopack itself skips the uninstall entry for portable apps.
        return None
    try:
        values = _read_sq_version(manifest)
    except Exception as exc:  # noqa: BLE001, RUF100 - unreadable manifest means "do not register"
        logger.warning("Uninstall entry check skipped: cannot read %s: %s", manifest, exc)
        return None
    pack_id = values.get("id", "")
    version = values.get("version", "")
    if not pack_id or not version:
        return None
    return VelopackInstallLayout(
        root_dir=str(root),
        update_exe=str(update_exe),
        main_exe=str(current_dir / (values.get("mainExe") or exe.name)),
        pack_id=pack_id,
        title=values.get("title") or pack_id,
        authors=values.get("authors") or values.get("title") or pack_id,
        version=version,
    )


def _directory_size_kb(root: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return min(total // 1024, 0xFFFFFFFF)


def uninstall_entry_values(
    layout: VelopackInstallLayout,
    *,
    size_kb: int,
    today: datetime.date | None = None,
) -> dict[str, tuple[str | int, str]]:
    """The values Velopack writes, as ``{name: (value, "sz" | "dword")}``."""
    day = today or datetime.date.today()
    update = layout.update_exe
    return {
        "DisplayIcon": (layout.main_exe, "sz"),
        "DisplayName": (layout.title, "sz"),
        "DisplayVersion": (layout.version, "sz"),
        "InstallDate": (day.strftime("%Y%m%d"), "sz"),
        "InstallLocation": (layout.root_dir, "sz"),
        "Publisher": (layout.authors, "sz"),
        "QuietUninstallString": ('"%s" --uninstall --silent' % update, "sz"),
        "UninstallString": ('"%s" --uninstall' % update, "sz"),
        "EstimatedSize": (int(size_kb), "dword"),
        "NoModify": (1, "dword"),
        "NoRepair": (1, "dword"),
        "Language": (0x0409, "dword"),
    }


def ensure_uninstall_entry(
    *,
    executable: str | None = None,
    winreg_module: Any = None,
    size_kb: Callable[[str], int] | None = None,
) -> str:
    """Recreate the per-user Settings > Apps entry if Velopack's key is missing.

    Returns what happened: ``"not_velopack"``, ``"present"``, ``"repaired"`` or
    ``"failed"``. Never raises, never touches HKLM, and never rewrites a key
    that already has an UninstallString (Velopack owns it then, and refreshes
    it on every update).
    """
    if winreg_module is None:
        if sys.platform != "win32":
            return "not_velopack"
        import winreg as winreg_module  # type: ignore[no-redef]
    winreg = winreg_module
    layout = detect_velopack_install_layout(executable)
    if layout is None:
        return "not_velopack"
    key_path = "%s\\%s" % (_UNINSTALL_REGISTRY_PATH, layout.pack_id)
    try:
        existing = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        logger.warning("Uninstall entry check failed for HKCU\\%s: %s", key_path, exc)
        return "failed"
    if existing is not None:
        try:
            winreg.QueryValueEx(existing, "UninstallString")
            return "present"
        except FileNotFoundError:
            pass  # an empty shell of a key shows nothing in Settings; fill it in
        except OSError as exc:
            logger.warning("Uninstall entry read failed for HKCU\\%s: %s", key_path, exc)
            return "failed"
        finally:
            winreg.CloseKey(existing)
    try:
        values = uninstall_entry_values(layout, size_kb=(size_kb or _directory_size_kb)(layout.root_dir))
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
        try:
            for name, (value, kind) in values.items():
                reg_type = winreg.REG_DWORD if kind == "dword" else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, value)
        finally:
            winreg.CloseKey(key)
    except Exception as exc:  # noqa: BLE001, RUF100 - repair is best effort, never a startup failure
        logger.warning("Could not recreate the Settings > Apps entry HKCU\\%s: %s", key_path, exc)
        return "failed"
    logger.warning(
        "Settings > Apps entry for %s %s was missing; recreated HKCU\\%s (install root %s)",
        layout.title,
        layout.version,
        key_path,
        layout.root_dir,
    )
    return "repaired"


def start_uninstall_entry_self_heal() -> bool:
    """Run :func:`ensure_uninstall_entry` once per process on a daemon thread.

    Sizing the install root walks a large tree, so it never runs on the UI
    thread. Returns False off Windows, in a dev run, or when it already ran.
    """
    global _UNINSTALL_SELF_HEAL_STARTED
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    with _UNINSTALL_SELF_HEAL_LOCK:
        if _UNINSTALL_SELF_HEAL_STARTED:
            return False
        _UNINSTALL_SELF_HEAL_STARTED = True
    threading.Thread(target=ensure_uninstall_entry, name="uninstall-entry-self-heal", daemon=True).start()
    return True


__all__ = [
    "ConsentRequiredError",
    "StageResult",
    "VelopackApplyError",
    "VelopackError",
    "VelopackInstallLayout",
    "VelopackNotInstalled",
    "VelopackUnavailable",
    "VelopackUpdater",
    "detect_velopack_install_layout",
    "ensure_uninstall_entry",
    "install_startup_hook",
    "start_uninstall_entry_self_heal",
    "uninstall_entry_values",
    "velopack_channel",
]
