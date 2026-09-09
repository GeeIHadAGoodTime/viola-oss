"""Non-modal desktop update banner for the Qt shell.

Handles two apply-policy paths (LOCKED founder policy 2026-06-12):

  OFFERED (default, routine release)
    The update has ALREADY been downloaded + signature-verified by
    VelopackUpdater.check_and_stage() before show_native_offered() is called.
    The banner shows "[Restart now]" and "[Later]".
    ONLY clicking "Restart now" calls apply_and_restart(..., user_consent=True).
    "Later" dismisses and defers back to the escalation ladder in UpdateCheckScheduler.

  FORCED (mandatory critical-safety / brick-fix only)
    The banner is NOT shown for the decision.  The caller auto-applies immediately via
    apply_and_restart(..., user_consent=False), showing only a brief non-blocking status
    message (show_native_forced_applying) while the relaunch proceeds.

Between "an update exists" and either of those sits show_native_staging(): the
download is hundreds of megabytes, so the banner reports it in progress with the
apply action disabled.  A "Restart now" button that appears before the package is
on disk can only ever fail — Velopack applies a file, not a URL.

The manual-reinstall path (native apply unavailable on this install, or staging
failed) uses show_available() / show_download_ready() to display a "Download
Update" button pointing at the website.  Every entry point resets the primary
button's label and its click wiring, so a banner that showed "Restart now"
earlier in the session can never leave the apply handler attached to a
manual-download banner.

This widget is pure UI plumbing: it holds a reference to a VelopackUpdater and an
UpdateCheckScheduler so it can call the right method on explicit user action.
It does not classify queries or parse model output.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

if TYPE_CHECKING:  # pragma: no cover - typing only
    from utils.update_checker import UpdateCheckScheduler
    from utils.velopack_updater import VelopackUpdater

logger = logging.getLogger(__name__)


class UpdateBanner(QWidget):
    """Compact banner with manual update actions that do not interrupt active use."""

    install_now_requested = Signal()
    install_on_quit_requested = Signal()
    remind_later_requested = Signal()

    # Fired when the user clicks "Restart now" on the native-apply offered path.
    # Carries the update manifest payload so connected slots can react (e.g. show
    # a spinner) before the process relaunches.
    restart_now_requested = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self.setFixedHeight(52)
        # Qt cannot read the React accent CSS variable, so use the approved bronze fallback.
        self.setStyleSheet("""
            QWidget#updateBanner {
                background-color: #1f1a12;
                border-bottom: 1px solid rgba(176, 141, 87, 0.32);
            }
            QLabel#updateBannerMessage {
                color: #ffffff;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton {
                background-color: rgba(255, 255, 255, 0.10);
                border: 1px solid rgba(255, 255, 255, 0.16);
                color: #ffffff;
                font-size: 12px;
                font-weight: 600;
                padding: 7px 12px;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.16);
            }
            QPushButton:disabled {
                color: rgba(255, 255, 255, 0.44);
                background-color: rgba(255, 255, 255, 0.05);
            }
            QPushButton#updateInstallNowButton {
                background-color: #B08D57;
                color: #ffffff;
                border-color: #D1B37A;
            }
            QPushButton#updateInstallNowButton:hover {
                background-color: #C4A46F;
            }
            """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)

        self.message = QLabel(self)
        self.message.setObjectName("updateBannerMessage")
        self.message.setText("")
        layout.addWidget(self.message, stretch=1)

        self.install_now_button = QPushButton("Download Update", self)
        self.install_now_button.setObjectName("updateInstallNowButton")
        self.install_now_button.clicked.connect(self.install_now_requested.emit)
        layout.addWidget(self.install_now_button)
        # What the primary button currently does: "manual" (open the download
        # page) or "apply" (consent-gated native apply). Tracked so switching
        # between the two paths within one session cannot leave the wrong
        # handler wired to the wrong label.
        self._primary_action_mode = "manual"

        self.install_on_quit_button = QPushButton("Install on Quit", self)
        self.install_on_quit_button.setObjectName("updateInstallOnQuitButton")
        self.install_on_quit_button.clicked.connect(self.install_on_quit_requested.emit)
        layout.addWidget(self.install_on_quit_button)

        self.remind_later_button = QPushButton("Remind Me Later", self)
        self.remind_later_button.setObjectName("updateRemindLaterButton")
        self.remind_later_button.clicked.connect(self.remind_later_requested.emit)
        layout.addWidget(self.remind_later_button)

        self.hide()

        # Held by the window after construction so the "Restart now" handler
        # can call apply_and_restart().  Both may be None when the native-apply
        # path is inactive (NATIVE_SELF_UPDATE_APPLY_SUPPORTED is False).
        self._velopack_updater: VelopackUpdater | None = None
        self._scheduler: UpdateCheckScheduler | None = None
        # The manifest from the last show_native_offered() call.
        self._current_native_manifest: dict[str, object] = {}
        # The installer URL from the last manual-reinstall payload, read by
        # download_url() when the user clicks "Download Update".
        self._current_download_url: object = ""

    # ------------------------------------------------------------------
    # Dependency injection (called by the window after construction)
    # ------------------------------------------------------------------

    def set_velopack_updater(self, updater: VelopackUpdater | None) -> None:
        """Wire the native-apply client so 'Restart now' can reach it."""
        self._velopack_updater = updater

    def set_scheduler(self, scheduler: UpdateCheckScheduler | None) -> None:
        """Wire the scheduler so 'Later' can call remind_later()."""
        self._scheduler = scheduler

    # ------------------------------------------------------------------
    # Manual-reinstall path (existing, unchanged behaviour)
    # ------------------------------------------------------------------

    def show_available(self, payload: dict[str, object]) -> None:
        """Show an update notice for the manual reinstall path."""
        version = str(payload.get("latest_version") or "a new version")
        self._set_message("Update %s is available. Download the latest installer to reinstall." % version)
        self._set_action_state(download_ready=True, payload=payload)
        self.show()

    def show_download_started(self, payload: dict[str, object]) -> None:
        """Compatibility shim for old staged-update events."""
        version = str(payload.get("latest_version") or "the update")
        self._set_message("Update %s is available. Download the latest installer to reinstall." % version)
        self._set_action_state(download_ready=True, payload=payload)
        self.show()

    def show_download_ready(self, payload: dict[str, object]) -> None:
        """Compatibility shim for old staged-update events."""
        version = str(payload.get("latest_version") or "the update")
        escalation = str(payload.get("escalation") or "normal")
        if escalation in {"required", "forced"}:
            text = "Critical update %s is available. Download the latest installer to reinstall." % version
        else:
            text = "Update %s is available. Download the latest installer to reinstall." % version
        self._set_message(text)
        self._set_action_state(download_ready=True, payload=payload)
        self.show()

    def show_install_on_quit(self, payload: dict[str, object]) -> None:
        """Compatibility shim; manual reinstall builds never stage installers."""
        version = str(payload.get("latest_version") or "the update")
        self._set_message("Update %s is available. Download the latest installer to reinstall." % version)
        self._current_download_url = payload.get("url") or payload.get("download_url")
        self._set_primary_action("manual")
        self.install_now_button.setEnabled(True)
        self.install_on_quit_button.setEnabled(False)
        self.install_on_quit_button.hide()
        self.remind_later_button.setEnabled(False)
        self.remind_later_button.show()
        self.show()

    def show_error(self, payload: dict[str, object]) -> None:
        """Show a low-noise staging error with a later reminder option."""
        message = str(payload.get("message") or "Update download failed.")
        self._set_message(message)
        self._set_primary_action("manual")
        self.install_now_button.setEnabled(False)
        self.install_on_quit_button.setEnabled(False)
        self.remind_later_button.setEnabled(True)
        self.remind_later_button.show()
        self.show()

    # ------------------------------------------------------------------
    # Native-apply path (active only when NATIVE_SELF_UPDATE_APPLY_SUPPORTED=True)
    # ------------------------------------------------------------------

    def show_native_staging(self, payload: dict[str, object]) -> None:
        """Show that the update is downloading, with no apply action yet.

        Staging (download + signature verify) can take minutes on a large
        release, and the user deserves to know it is happening.  The primary
        button stays DISABLED for the whole of it: Velopack applies a package
        that is already on disk, so an enabled "Restart now" before staging
        completes is a button whose only possible outcome is a failure.
        """
        version = str(payload.get("latest_version") or "a new version")
        # Not applyable yet — nothing may reach apply_and_restart from here.
        self._current_native_manifest = {}
        self._set_message("Downloading Viola %s..." % version)
        self._set_primary_action("apply")
        self.install_now_button.setEnabled(False)
        self.install_on_quit_button.setEnabled(False)
        self.install_on_quit_button.hide()
        self._set_remind_later_state(payload)
        self.show()

    def show_native_offered(self, payload: dict[str, object]) -> None:
        """Show the OFFERED banner: "[Restart now]" + "[Later]".

        The update has already been downloaded and signature-verified by
        VelopackUpdater.check_and_stage() before this is called.  The banner
        only applies when the user explicitly clicks "Restart now" — never on
        its own.

        This method is only reached when NATIVE_SELF_UPDATE_APPLY_SUPPORTED is
        True AND the package for this version is staged on disk.
        """
        version = str(payload.get("latest_version") or "a new version")
        self._current_native_manifest = dict(payload)

        self._set_message("Viola %s is ready." % version)

        # Switch "Download Update" to "Restart now" and wire the consent-gated
        # apply handler.
        self._set_primary_action("apply")
        self.install_now_button.setEnabled(True)

        # Install-on-quit is not applicable to the native-apply path.
        self.install_on_quit_button.setEnabled(False)
        self.install_on_quit_button.hide()

        # "Later" emits remind_later_requested; the window connects it to
        # scheduler.remind_later() after calling set_scheduler().
        self._set_remind_later_state(payload)

        self.show()

    def show_native_forced_applying(self, payload: dict[str, object]) -> None:
        """Show a brief status for a FORCED (mandatory) auto-apply in progress.

        No buttons — this is informational only.  The caller has already
        invoked apply_and_restart(..., user_consent=False) which will relaunch
        the app momentarily.

        This method is only reached when the update policy resolves to FORCED
        (closed safety/brick class with a valid mandatory_reason in the
        MANDATORY_REASON_ALLOWLIST defined in updates/update_policy.py).
        """
        version = str(payload.get("latest_version") or "")
        if version:
            self._set_message("Applying a critical update (%s)..." % version)
        else:
            self._set_message("Applying a critical update...")

        self.install_now_button.setEnabled(False)
        self.install_on_quit_button.setEnabled(False)
        self.install_on_quit_button.hide()
        self.remind_later_button.setEnabled(False)
        self.remind_later_button.hide()

        self.show()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_restart_now_clicked(self) -> None:
        """Handle the "Restart now" button click on the offered native-apply path.

        This is the ONLY place that calls apply_and_restart with user_consent=True.
        It requires:
        1. A VelopackUpdater has been injected (set_velopack_updater was called).
        2. A manifest from the last show_native_offered() call exists.

        If either is missing the click is a no-op with a warning log — it never
        silently relaunches without both.  The guard is intentional: a click
        without a wired updater means NATIVE_SELF_UPDATE_APPLY_SUPPORTED is False
        and we must not apply anything.
        """
        if self._velopack_updater is None:
            logger.warning(
                "UpdateBanner: Restart now clicked but no VelopackUpdater is wired; "
                "cannot apply update (NATIVE_SELF_UPDATE_APPLY_SUPPORTED may be False)"
            )
            return
        if not self._current_native_manifest:
            logger.warning("UpdateBanner: Restart now clicked but no manifest is held; cannot apply update")
            return

        manifest = self._current_native_manifest
        # Emit the signal before applying so the window can react (e.g. show a
        # status) before the process relaunches.
        self.restart_now_requested.emit(dict(manifest))
        try:
            self._velopack_updater.apply_and_restart(manifest, user_consent=True)
        except Exception as exc:  # noqa: BLE001, RUF100
            logger.error("UpdateBanner: apply_and_restart failed: %s", exc)
            self._show_apply_failed_fallback(manifest, exc)

    def _show_apply_failed_fallback(self, manifest: dict[str, object], exc: Exception) -> None:
        """CONFIRM-4 fallback: the automatic apply failed (e.g. the Velopack feed
        is unreachable, DNS failure, timeout, or a version-pin mismatch) — none
        of which the user can retry their way out of by clicking "Restart now"
        again. Route to the existing manual-reinstall surface (`show_error`)
        with the real download URL instead of leaving a dead "Restart now"
        button behind a message the user cannot act on.

        This does not retry automatically: the routine 4-hour scheduler check
        already re-polls the feed on its own (CONFIRM-4's recommended
        behaviour), so this method's only job is to give the user an honest,
        actionable way forward right now — never a fake success.
        """
        from utils.update_checker import safe_update_download_url

        # The manifest's "url" is normally already sanitised by
        # check_for_update() (SEC-016), but every consumer re-sanitises at its
        # own point of use rather than trusting an upstream caller.
        download_url = safe_update_download_url(manifest.get("url") or manifest.get("download_url"))
        version = str(manifest.get("latest_version") or "the update")
        self.show_error(
            {
                "message": (
                    "Could not automatically apply %s (%s). Download the latest "
                    "installer manually from %s." % (version, exc, download_url)
                )
            }
        )

    def _set_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setToolTip(text)

    def _set_primary_action(self, mode: str) -> None:
        """Point the primary button at exactly one handler, and label it to match.

        The two paths share one button, so the label and the connected slot must
        move together.  Doing this in one place is what stops a session that
        showed "Restart now" from later showing "Download Update" while still
        wired to the apply handler (or the reverse): the click always does what
        the label says.
        """
        if mode not in ("manual", "apply"):
            raise ValueError("unknown primary action mode: %r" % mode)
        try:
            self.install_now_button.clicked.disconnect()
        except (RuntimeError, TypeError):
            # No connection to drop (Qt raises RuntimeError; PySide may raise
            # TypeError when the signal has no receivers).
            pass
        if mode == "apply":
            self.install_now_button.setText("Restart now")
            self.install_now_button.clicked.connect(self._on_restart_now_clicked)
        else:
            self.install_now_button.setText("Download Update")
            self.install_now_button.clicked.connect(self.install_now_requested.emit)
        self._primary_action_mode = mode

    def _set_remind_later_state(self, payload: dict[str, object]) -> None:
        """Show "Later" unless the escalation ladder says a decision is due."""
        escalation = str(payload.get("escalation") or "normal")
        can_remind = escalation not in {"required", "forced"}
        self.remind_later_button.setEnabled(can_remind)
        self.remind_later_button.setVisible(can_remind)

    def download_url(self) -> str:
        """The sanitised installer URL the manual "Download Update" button opens.

        Falls back to the public download page when the payload carried nothing
        usable, so the button always has somewhere honest to send the user.
        """
        from utils.update_checker import safe_update_download_url

        return safe_update_download_url(self._current_download_url)

    def _set_action_state(self, *, download_ready: bool, payload: dict[str, object]) -> None:
        # Manual-reinstall surface: always restore the manual label + handler so
        # a banner that previously offered a native apply cannot leave the apply
        # handler behind a "Download Update" button.
        self._current_download_url = payload.get("url") or payload.get("download_url")
        self._set_primary_action("manual")
        self.install_now_button.setEnabled(download_ready)
        self.install_on_quit_button.setEnabled(False)
        self.install_on_quit_button.hide()
        self._set_remind_later_state(payload)


__all__ = ["UpdateBanner"]
