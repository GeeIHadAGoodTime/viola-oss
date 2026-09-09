"""Desktop update verification helpers for packaged Windows builds.

Launch updates are manual reinstall only: Viola checks the public manifest and
points users to the signed installer, but it does not download or launch the
installer automatically. Verification helpers remain here so installer hashes
and Authenticode identity can be tested during release smoke checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from core.logging_config import get_logger
from utils.update_checker import NATIVE_SELF_UPDATE_APPLY_SUPPORTED, UPDATE_APPLY_BOUNDARY, check_for_update

if TYPE_CHECKING:
    from PySide6.QtWidgets import QSystemTrayIcon

logger = get_logger(__name__)

DEFAULT_INSTALLER_URL = "https://useviola.com/download/latest.exe"
# Azure Trusted Signing issues a FRESH short-lived leaf certificate on every signing
# operation, so the leaf thumbprint rotates per build (observed in
# scripts/installer_smoke.py: 85B5310F on public 1.0.1, E39BA9B6 in REQUAL3, 61393B5B
# on 1.0.2 -- same identity each time). A single baked default thumbprint therefore
# false-fails every fresh build (#1545). The durable identity proof mirrors
# scripts/installer_smoke.py and utils/macos_updater.py: Authenticode status Valid,
# the signer SUBJECT is our identity, AND the ISSUER is the Microsoft Trusted Signing
# CA -- never a baked per-build thumbprint. VIOLA_UPDATE_SIGNING_THUMBPRINT remains
# available as an OPTIONAL defense-in-depth pin (e.g. to hard-lock one rotation
# window); it is unset by default so a routine release never false-fails.
DEFAULT_SIGNING_SUBJECT = "Jihad Shkoukani"
DEFAULT_SIGNING_ISSUER_FRAGMENT = "Microsoft ID Verified"
DEFAULT_DELAY_SECONDS = 45
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


class UpdateSecurityError(RuntimeError):
    """Raised when a downloaded update artifact fails verification."""


@dataclass(frozen=True)
class SignatureInfo:
    status: str
    thumbprint: str
    subject: str
    issuer: str = ""


@dataclass(frozen=True)
class UpdateResult:
    status: str
    detail: str
    version: str = ""
    path: str = ""
    apply_supported: bool = NATIVE_SELF_UPDATE_APPLY_SUPPORTED
    update_apply_boundary: str = UPDATE_APPLY_BOUNDARY


def schedule_background_auto_update(
    *,
    tray_icon: QSystemTrayIcon | None = None,
    manifest_url: str | None = None,
    delay_seconds: int = DEFAULT_DELAY_SECONDS,
    settings_getter: Callable[[str, object], object] | None = None,
    quit_callback: Callable[[], None] | None = None,
) -> threading.Thread | None:
    """Schedule one non-blocking manual-update notification for packaged builds."""
    if not _should_schedule_updates():
        logger.debug("Desktop update scheduler skipped outside packaged Windows build")
        return None

    def _run_after_delay() -> None:
        time.sleep(max(0, delay_seconds))
        result = check_and_apply_update(
            tray_icon=tray_icon,
            manifest_url=manifest_url,
            settings_getter=settings_getter,
            quit_callback=quit_callback,
        )
        logger.debug("Desktop update check result: %s (%s)", result.status, result.detail)

    thread = threading.Thread(target=_run_after_delay, daemon=True, name="desktop-update-checker")
    thread.start()
    logger.debug("Desktop update check scheduled (delay=%ds)", delay_seconds)
    return thread


def check_and_apply_update(
    *,
    tray_icon: QSystemTrayIcon | None = None,
    manifest_url: str | None = None,
    settings_getter: Callable[[str, object], object] | None = None,
    quit_callback: Callable[[], None] | None = None,
) -> UpdateResult:
    """Check the manifest, prompt manual reinstall when needed, and never raise to UI."""
    _ = settings_getter  # Kept for compatibility with older update callers.
    try:
        manifest = check_for_update(manifest_url)
    except httpx.HTTPError as exc:
        logger.debug("Update manifest request failed: %s", exc)
        return UpdateResult("failed", "manifest_network_error")
    except Exception as exc:
        logger.debug("Update manifest check failed: %s", exc)
        return UpdateResult("failed", "manifest_error")

    latest_version = str(manifest.get("latest_version", "") or "")
    required = _force_update_required(manifest)
    if not manifest.get("available") and not required:
        return UpdateResult("noop", "up_to_date", version=latest_version)

    download_url = _installer_url(manifest)
    title = "Viola Update Required" if required else "Viola Update Available"
    _show_tray_message(
        tray_icon,
        title,
        "Download the latest installer from useviola.com/download and reinstall Viola.",
    )
    if quit_callback is not None and required:
        quit_callback()
    return UpdateResult(
        "manual_required" if required else "manual_available",
        "manual_reinstall",
        version=latest_version,
        path=download_url,
    )


def verify_downloaded_installer(
    installer_path: Path,
    *,
    expected_sha256: str,
    expected_subject: str | None = None,
    expected_issuer_fragment: str | None = None,
    expected_thumbprint: str | None = None,
) -> SignatureInfo:
    """Verify the installer hash and Authenticode identity before execution."""
    _verify_sha256(installer_path, expected_sha256)
    return verify_authenticode_signature(
        installer_path,
        expected_subject=expected_subject or _expected_signing_subject(),
        expected_issuer_fragment=expected_issuer_fragment or _expected_signing_issuer_fragment(),
        expected_thumbprint=expected_thumbprint,
    )


def verify_authenticode_signature(
    target: Path,
    *,
    expected_subject: str | None = None,
    expected_issuer_fragment: str | None = None,
    expected_thumbprint: str | None = None,
) -> SignatureInfo:
    """Verify Authenticode status and signer IDENTITY (subject + issuer).

    Identity, not a per-build thumbprint, is the durable proof: Azure Trusted
    Signing mints a fresh leaf certificate on every signing operation, so the
    thumbprint is expected to differ release to release (see the module docstring
    / #1545). ``expected_thumbprint`` (or the ``VIOLA_UPDATE_SIGNING_THUMBPRINT``
    env override) is OPTIONAL extra pinning for a caller that wants to hard-lock
    one specific rotation window; when neither is supplied, only identity is
    checked, which is what a routine release must pass.
    """
    if os.name != "nt":
        raise UpdateSecurityError("Authenticode verification requires Windows")
    if not target.exists():
        raise UpdateSecurityError("Update artifact does not exist: %s" % target)

    signature = _read_authenticode_signature(target)
    if signature.status != "Valid":
        raise UpdateSecurityError("%s signature is not valid: status=%s" % (target.name, signature.status))

    expected_subject_value = expected_subject or _expected_signing_subject()
    if expected_subject_value.lower() not in signature.subject.lower():
        raise UpdateSecurityError(
            "%s signature subject mismatch: expected_contains=%r actual=%r"
            % (target.name, expected_subject_value, signature.subject)
        )

    expected_issuer_value = expected_issuer_fragment or _expected_signing_issuer_fragment()
    if expected_issuer_value.lower() not in signature.issuer.lower():
        raise UpdateSecurityError(
            "%s signature issuer is not the expected Trusted Signing CA: expected_contains=%r actual=%r"
            % (target.name, expected_issuer_value, signature.issuer or "missing")
        )

    thumbprint_override = expected_thumbprint or os.environ.get("VIOLA_UPDATE_SIGNING_THUMBPRINT", "").strip()
    if thumbprint_override:
        normalised_override = _normalise_thumbprint(thumbprint_override)
        if signature.thumbprint != normalised_override:
            raise UpdateSecurityError(
                "%s signature thumbprint mismatch against explicit pin: expected=%s actual=%s"
                % (target.name, normalised_override, signature.thumbprint or "missing")
            )

    return signature


def _should_schedule_updates() -> bool:
    if os.environ.get("VIOLA_AUTO_UPDATE_DEV", "").strip().lower() in TRUE_VALUES:
        return True
    if os.environ.get("VIOLA_AUTO_UPDATE", "").strip().lower() in FALSE_VALUES:
        return False
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def _force_update_required(manifest: dict[str, object]) -> bool:
    min_supported = str(manifest.get("min_supported", "") or "").strip()
    current = str(manifest.get("current_version", "") or "").strip()
    if not min_supported or not current:
        return False
    from utils.update_checker import _parse_version

    return _parse_version(current) < _parse_version(min_supported)


def _installer_url(manifest: dict[str, object]) -> str:
    value = str(
        manifest.get("url")
        or manifest.get("installer_url")
        or os.environ.get("VIOLA_UPDATE_INSTALLER_URL", DEFAULT_INSTALLER_URL)
    ).strip()
    if not value.lower().startswith("https://"):
        raise UpdateSecurityError("Installer updates require HTTPS URLs")
    return value


def _read_authenticode_signature(target: Path) -> SignatureInfo:
    # The target path is handed to PowerShell through an ENVIRONMENT VARIABLE
    # ($env:VIOLA_SIG_TARGET), never a trailing argv positional. On Windows
    # PowerShell 5.1 a plain-string ``-Command`` does NOT bind trailing argv into
    # the ``$args`` automatic variable, so the pre-fix ``$args[0]`` shape resolved
    # to $null and ``Get-AuthenticodeSignature -LiteralPath $null`` threw under
    # ErrorActionPreference=Stop -- an uncaught CalledProcessError BEFORE the
    # signer-identity comparison in verify_authenticode_signature ever ran, i.e.
    # update signature verification silently failed open (#3497 / #2490 gap).
    # The env-var channel binds identically on PS 5.1 and PS 7 and, unlike a
    # double-quoted trailing arg, is read as a literal string ($env: values are
    # never re-parsed as PowerShell), so a crafted path cannot inject via $(...).
    script = (
        "$ErrorActionPreference='Stop';"
        "$sig=Get-AuthenticodeSignature -LiteralPath $env:VIOLA_SIG_TARGET;"
        "$cert=$sig.SignerCertificate;"
        "$thumb=if($cert){$cert.Thumbprint}else{''};"
        "$subject=if($cert){$cert.Subject}else{''};"
        "$issuer=if($cert){$cert.Issuer}else{''};"
        "[pscustomobject]@{Status=[string]$sig.Status;Thumbprint=$thumb;Subject=$subject;Issuer=$issuer}|"
        "ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        cwd=str(target.parent),
        env={**os.environ, "VIOLA_SIG_TARGET": str(target)},
        text=True,
        timeout=30.0,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UpdateSecurityError("Could not parse Authenticode signature output") from exc

    if not isinstance(payload, dict):
        raise UpdateSecurityError("Unexpected Authenticode signature output")
    return SignatureInfo(
        status=str(payload.get("Status", "")),
        thumbprint=_normalise_thumbprint(str(payload.get("Thumbprint", ""))),
        subject=str(payload.get("Subject", "")),
        issuer=str(payload.get("Issuer", "")),
    )


def _verify_sha256(path: Path, expected_sha256: str) -> None:
    expected = _normalise_sha256(expected_sha256)
    if not expected:
        raise UpdateSecurityError("Expected SHA-256 is missing")
    actual = sha256_file(path)
    if actual != expected:
        raise UpdateSecurityError("%s SHA-256 mismatch: expected=%s actual=%s" % (path.name, expected, actual))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _expected_signing_subject() -> str:
    return os.environ.get("VIOLA_UPDATE_SIGNING_SUBJECT", DEFAULT_SIGNING_SUBJECT).strip() or DEFAULT_SIGNING_SUBJECT


def _expected_signing_issuer_fragment() -> str:
    return (
        os.environ.get("VIOLA_UPDATE_SIGNING_ISSUER_FRAGMENT", DEFAULT_SIGNING_ISSUER_FRAGMENT).strip()
        or DEFAULT_SIGNING_ISSUER_FRAGMENT
    )


def _normalise_thumbprint(value: str) -> str:
    return value.replace(" ", "").replace(":", "").upper()


def _normalise_sha256(value: str) -> str:
    cleaned = value.strip().replace(" ", "").upper()
    if len(cleaned) == 64 and all(char in "0123456789ABCDEF" for char in cleaned):
        return cleaned
    return ""


def _show_tray_message(tray_icon: QSystemTrayIcon | None, title: str, message: str) -> None:
    if tray_icon is None:
        return
    try:
        from PySide6.QtWidgets import QSystemTrayIcon

        tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 10000)
    except Exception as exc:
        logger.debug("Could not show update tray notification: %s", exc)


__all__ = [
    "SignatureInfo",
    "UpdateResult",
    "UpdateSecurityError",
    "check_and_apply_update",
    "schedule_background_auto_update",
    "sha256_file",
    "verify_authenticode_signature",
    "verify_downloaded_installer",
]
