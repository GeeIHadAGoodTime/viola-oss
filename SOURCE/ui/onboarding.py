"""
Onboarding System - First-Run Experience
Guides new users through initial setup and feature discovery

Features:
- Multi-step wizard
- Feature tutorials
- Interactive guidance
- Progress tracking
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from html import escape
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from ui.security.bootstrap import (
    is_bootstrap_key_acknowledged,
    load_bootstrap_api_key,
    mark_bootstrap_key_acknowledged,
)

logger = get_logger(__name__)

_ONBOARDING_STATE_KEYS = frozenset({"onboarding_completed", "onboarding_completed_at"})
_AUTH_USER_STATE_TABLES = (
    "users",
    "sessions",
    "oauth_identities",
    "oauth_tokens",
    "user_settings",
    "user_profiles",
    "user_models",
    "user_preferences",
    "user_credentials",
    "webauthn_credentials",
    "mfa_totp",
    "subscriptions",
)


class OnboardingStep(Enum):
    """Steps in the onboarding flow"""

    WELCOME = "welcome"
    API_ACCESS = "api_access"
    VOICE_MODE = "voice_mode"
    MICROPHONE_TEST = "microphone_test"
    AI_SETUP = "ai_setup"
    QUICK_TUTORIAL = "quick_tutorial"
    COMPLETE = "complete"


@dataclass
class StepConfig:
    """Configuration for an onboarding step"""

    id: OnboardingStep
    title: str
    description: str
    content: str  # HTML or rich text description
    icon: str
    primary_action: str  # Button label
    secondary_action: str | None = None  # Optional skip/back button
    can_skip: bool = True
    validation_callback: str | None = None  # JS function to validate before continuing
    on_complete: Callable | None = None  # Python callback when step completes


@dataclass
class TutorialTip:
    """Individual tutorial tip/command example"""

    command: str
    description: str
    icon: str
    category: str  # "music", "questions", "control"


class OnboardingSystem:
    """
    Manages first-run onboarding experience.
    Tracks progress and provides guided setup.
    """

    def __init__(self, settings_manager):
        self.settings_manager = settings_manager
        self._bootstrap_key = load_bootstrap_api_key()
        self._bootstrap_ack = is_bootstrap_key_acknowledged()
        self._steps = self._build_steps()
        self._current_step = 0

    def _build_steps(self) -> list[StepConfig]:
        """Build onboarding step sequence"""
        steps = [
            # Step 1: Welcome
            StepConfig(
                id=OnboardingStep.WELCOME,
                title="Welcome to Viola! 🎵",
                description="Your AI-powered voice assistant for music and more",
                content="""
                <div class="onboarding-welcome">
                    <h2>Hi! I'm Viola 👋</h2>
                    <p>I can help you with:</p>
                    <ul class="feature-list">
                        <li>🎵 <strong>Play Music</strong> - Search and play from YouTube</li>
                        <li>💬 <strong>Answer Questions</strong> - Ask me anything</li>
                        <li>🎙️ <strong>Voice Control</strong> - Hands-free operation</li>
                        <li>📅 <strong>Calendar & Weather</strong> - Stay organized</li>
                    </ul>
                    <p>Let's get you set up! This will only take a minute.</p>
                </div>
                """,
                icon="🎵",
                primary_action="Let's Go!",
                secondary_action=None,
                can_skip=False,
            ),
        ]

        if self._bootstrap_key and not self._bootstrap_ack:
            key_html = escape(self._bootstrap_key)
            steps.append(
                StepConfig(
                    id=OnboardingStep.API_ACCESS,
                    title="Secure Your API Access",
                    description="Use this API key to authorize the Viola UI and CLI clients.",
                    content=f"""
                    <div class="onboarding-security">
                        <h3>Your Viola API Key</h3>
                        <p class="security-instructions">
                            Copy this key into any client that connects to Viola's API.
                            It is shown once here and stored in <code>data/secrets/initial_api_key</code>.
                        </p>
                        <div class="security-key">
                            <code>{key_html}</code>
                        </div>
                        <p class="security-warning">
                            ⚠️ Treat this like a password. Rotate it with <code>python run_viola.py --reset-auth</code> if it leaks.
                        </p>
                    </div>
                    """,
                    icon="🔑",
                    primary_action="Got it",
                    secondary_action="Back",
                    can_skip=False,
                )
            )

        steps.extend(
            [
                # Step 2: Voice Mode Selection
                StepConfig(
                    id=OnboardingStep.VOICE_MODE,
                    title="Choose Your Voice Mode",
                    description="How would you like to give voice commands?",
                    content="""
                <div class="onboarding-voice-mode" role="radiogroup" aria-label="Voice mode">
                    <!-- Pre-selection must match config/defaults.py DEFAULT_VOICE_MODE,
                         or this screen offers the user a mode their install is not in. -->
                    <button type="button" class="mode-option" data-mode="wake_word" role="radio" aria-checked="true">
                        <div class="mode-icon">🎤</div>
                        <h3>Wake Word</h3>
                        <p class="mode-description">Say "Viola" to activate</p>
                        <ul class="mode-pros">
                            <li>✓ Hands-free control</li>
                            <li>✓ Like Alexa or Siri</li>
                            <li>✓ Convenient when busy</li>
                        </ul>
                        <div class="recommendation-badge">Recommended</div>
                    </button>

                    <button type="button" class="mode-option" data-mode="push_to_talk" role="radio" aria-checked="false">
                        <div class="mode-icon">⌨️</div>
                        <h3>Push-to-Talk</h3>
                        <p class="mode-description">Press and hold <kbd>Space</kbd> to speak</p>
                        <ul class="mode-pros">
                            <li>✓ No accidental triggers</li>
                            <li>✓ Works immediately</li>
                            <li>✓ Great for noisy environments</li>
                        </ul>
                    </button>

                    <p class="mode-hint">💡 You can change this anytime — just say "switch to text mode" or "switch to voice mode"</p>
                </div>
                """,
                    icon="🎙️",
                    primary_action="Continue",
                    secondary_action="Back",
                    can_skip=True,
                ),
                # Step 3: Microphone Test
                StepConfig(
                    id=OnboardingStep.MICROPHONE_TEST,
                    title="Test Your Microphone",
                    description="Let's make sure your microphone is working",
                    content="""
                <div class="onboarding-mic-test">
                    <div class="mic-visual">
                        <div class="mic-icon">🎤</div>
                        <div class="audio-bars">
                            <div class="bar"></div>
                            <div class="bar"></div>
                            <div class="bar"></div>
                            <div class="bar"></div>
                            <div class="bar"></div>
                        </div>
                    </div>

                    <div class="mic-instructions">
                        <p><strong>Click the microphone and say something</strong></p>
                        <p class="hint">Try: "Play some jazz music"</p>
                    </div>

                    <div class="mic-status" id="mic-status" role="status" aria-live="polite">
                        <span class="status-icon">⏹️</span>
                        <span class="status-text">Click to start</span>
                    </div>

                    <div class="mic-result" id="mic-result" style="display: none;" role="status" aria-live="polite">
                        <div class="result-success">
                            <span class="result-icon">✓</span>
                            <p>I heard you say:</p>
                            <div class="transcript" id="mic-transcript"></div>
                        </div>
                    </div>

                    <button type="button" class="btn-mic-test" id="btn-mic-test" aria-describedby="mic-status">
                        <span class="btn-icon">🎤</span>
                        Test Microphone
                    </button>

                    <div class="troubleshooting-link">
                        <a href="#" id="mic-troubleshooting">Microphone not working?</a>
                    </div>
                </div>
                """,
                    icon="🎤",
                    primary_action="Continue",
                    secondary_action="Skip",
                    can_skip=True,
                    validation_callback="validateMicrophoneTest",
                ),
                # Step 4: AI Setup (Optional)
                StepConfig(
                    id=OnboardingStep.AI_SETUP,
                    title="Enable AI Features (Optional)",
                    description="Get smarter responses with your own AI key",
                    content="""
                <div class="onboarding-ai-setup">
                    <div class="feature-preview">
                        <h3>With AI Enabled:</h3>
                        <ul class="ai-features">
                            <li>💬 Natural conversations & questions</li>
                            <li>🎵 Smart music recommendations</li>
                            <li>🧠 Context-aware responses</li>
                            <li>✨ Personality and wit</li>
                        </ul>
                    </div>

                    <div class="api-key-input">
                        <label for="onboard-api-key">OpenAI API Key (BYOK):</label>
                        <input
                            type="password"
                            id="onboard-api-key"
                            class="setting-input"
                            aria-describedby="onboard-api-key-hint"
                            placeholder="sk-...">
                        <p class="input-hint" id="onboard-api-key-hint">
                            <a href="https://platform.openai.com/api-keys" target="_blank">
                                OpenAI API keys
                            </a>
                            are optional. If you paste one, prompts using this source go directly
                            from this device to OpenAI under your OpenAI account.
                        </p>
                    </div>

                    <div class="cost-info">
                        <p class="info-icon">ℹ️</p>
                        <p><strong>Provider billing:</strong> OpenAI bills your account for BYOK usage. Viola does not control those rates; check OpenAI's current pricing before enabling this path.</p>
                    </div>

                    <div class="skip-option">
                        <p>Don't have an API key? No problem.</p>
                        <p>Viola works for local commands and music without a BYOK key.</p>
                    </div>
                </div>
                """,
                    icon="🤖",
                    primary_action="Save & Continue",
                    secondary_action="Skip for Now",
                    can_skip=True,
                ),
                # Step 5: Quick Tutorial
                StepConfig(
                    id=OnboardingStep.QUICK_TUTORIAL,
                    title="Try These Commands",
                    description="Here are some things you can say or type",
                    content="""
                <div class="onboarding-tutorial">
                    <p class="tutorial-intro">Click any command to try it:</p>

                    <div class="command-examples">
                        <div class="command-category">
                            <h4>🎵 Music Control</h4>
                            <button type="button" class="example-cmd" data-cmd="play bohemian rhapsody">
                                "Play Bohemian Rhapsody"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="play some jazz">
                                "Play some jazz"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="volume 50">
                                "Volume 50"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="next song">
                                "Next song"
                            </button>
                        </div>

                        <div class="command-category">
                            <h4>💬 Questions & Chat</h4>
                            <button type="button" class="example-cmd" data-cmd="what's the weather">
                                "What's the weather?"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="tell me a joke">
                                "Tell me a joke"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="what can you do">
                                "What can you do?"
                            </button>
                        </div>

                        <div class="command-category">
                            <h4>⚡ Quick Actions</h4>
                            <button type="button" class="example-cmd" data-cmd="pause">
                                "Pause"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="resume">
                                "Resume"
                            </button>
                            <button type="button" class="example-cmd" data-cmd="what's playing">
                                "What's playing?"
                            </button>
                        </div>
                    </div>

                    <div class="keyboard-shortcuts">
                        <h4>⌨️ Keyboard Shortcuts</h4>
                        <ul>
                            <li><kbd>Space</kbd> - Push to talk (hold)</li>
                            <li><kbd>Ctrl</kbd>+<kbd>→</kbd> - Next track</li>
                            <li><kbd>Ctrl</kbd>+<kbd>←</kbd> - Previous track</li>
                            <li><kbd>F1</kbd> or <kbd>?</kbd> - Show all shortcuts</li>
                        </ul>
                    </div>
                </div>
                """,
                    icon="🎓",
                    primary_action="Start Using Viola!",
                    secondary_action="Back",
                    can_skip=False,
                ),
            ]
        )

        return steps

    def is_onboarding_complete(self) -> bool:
        """Check if user has completed onboarding.

        Existing installs that predate the onboarding flag can be migrated to
        complete, but only from persisted user state. Default runtime settings
        such as ``voice_mode`` or ``enable_gpt`` are not evidence of prior use.
        """
        completed = self.settings_manager.get("onboarding_completed", None)
        if completed is not None:
            result = bool(completed)
            logger.info(
                "ONBOARDING_DECISION outcome=%s reason=persisted_flag value=%s",
                "completed" if result else "show_onboarding",
                completed,
            )
            return result

        # Key not present — could be a fresh install or an existing user
        # who predates the onboarding feature.  Check for evidence of prior use.
        if self._detect_existing_user():
            logger.info("ONBOARDING_DECISION outcome=completed reason=existing_user_detected value=n/a")
            logger.info("Persisted user state detected; auto-completing onboarding")
            self.settings_manager.set("onboarding_completed", True)
            return True

        # Fresh install — show onboarding
        logger.info("ONBOARDING_DECISION outcome=show_onboarding reason=fresh_install value=n/a")
        return False

    def _detect_existing_user(self) -> bool:
        """Return True when persisted user state exists from a prior install."""
        evidence_keys = [
            "active_music_provider_id",
            "custom_wake_word_name",
            "delivery_address",
            "home_assistant_url",
            "llm_api_key",
            "llm_base_url",
            "local_music_folder",
            "openai_api_key",
            "telegram_owner_chat_id",
            # NOTE: telemetry_install_id is deliberately NOT evidence. It is
            # machine-minted on first boot (utils/update_checker.py's staged-
            # rollout check calls get_or_create_install_id() before the UI ever
            # asks for onboarding status), so counting it made every truly
            # fresh install look like an existing user and silently skip
            # first-run onboarding. Evidence keys must be USER-AUTHORED state.
            "user_phone_number",
            "wake_word_active_model",
            "wake_word_model",
        ]
        return (
            self._has_non_default_persisted_settings(evidence_keys)
            or self._has_secure_settings_cache()
            or self._has_auth_user_state()
        )

    def _has_non_default_persisted_settings(self, evidence_keys: list[str]) -> bool:
        """Detect user-authored settings without treating defaults as state."""
        persisted = self._read_persisted_settings()
        if not persisted:
            return False

        defaults = getattr(self.settings_manager, "DEFAULT_SETTINGS", {})
        for key in evidence_keys:
            if key not in persisted:
                continue
            value = persisted[key]
            if self._is_onboarding_state_key(key):
                continue
            if self._is_empty_prior_use_value(value):
                continue
            if key in defaults and value == defaults[key]:
                continue
            return True
        return False

    def _read_persisted_settings(self) -> dict[str, Any]:
        settings_file = getattr(self.settings_manager, "settings_file", None)
        if not settings_file:
            return {}

        path = Path(settings_file)
        if not path.exists():
            return {}

        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Unable to inspect persisted onboarding settings: %s", exc)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _is_onboarding_state_key(key: str) -> bool:
        return key in _ONBOARDING_STATE_KEYS or key.startswith("onboarding_step_")

    @staticmethod
    def _is_empty_prior_use_value(value: object) -> bool:
        if value is None or value is False:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        if isinstance(value, (list, tuple, set, dict)) and len(value) == 0:
            return True
        return False

    def _has_secure_settings_cache(self) -> bool:
        encrypted_cache_path = getattr(self.settings_manager, "_encrypted_cache_path", None)
        if not encrypted_cache_path:
            return False
        return Path(encrypted_cache_path).exists()

    def _has_auth_user_state(self) -> bool:
        db_path = self._auth_db_path()
        if db_path is None or not db_path.exists():
            return False

        try:
            with sqlite3.connect(str(db_path)) as conn:
                for table in _AUTH_USER_STATE_TABLES:
                    if table == "user_preferences":
                        # Machine bookkeeping rows (e.g. the "__settings_migrated__"
                        # marker written the moment a user signs in) are NOT
                        # user-authored state. Counting them auto-completed
                        # onboarding for anyone who signed in at step 1 and then
                        # restarted the app mid-flow — same bug class as the
                        # telemetry_install_id evidence key.
                        if self._user_preferences_has_user_rows(conn):
                            return True
                        continue
                    if self._sqlite_table_has_rows(conn, table):
                        return True
        except sqlite3.DatabaseError as exc:
            logger.debug("Unable to inspect auth DB for onboarding state: %s", exc)
        return False

    def _auth_db_path(self) -> Path | None:
        settings_file = getattr(self.settings_manager, "settings_file", None)
        if settings_file:
            return Path(settings_file).parent / "auth.db"
        return None

    @staticmethod
    def _user_preferences_has_user_rows(conn: sqlite3.Connection) -> bool:
        """True when user_preferences holds a USER-authored row.

        Rows whose key starts with a double underscore are internal
        bookkeeping markers minted by the app itself and must never count as
        prior-install evidence.
        """
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_preferences'",
        ).fetchone()
        if not table_exists:
            return False
        row = conn.execute(
            "SELECT 1 FROM user_preferences WHERE key NOT LIKE '\\_\\_%' ESCAPE '\\' LIMIT 1",
        ).fetchone()
        return row is not None

    @staticmethod
    def _sqlite_table_has_rows(conn: sqlite3.Connection, table: str) -> bool:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not table_exists:
            return False
        query = f'SELECT 1 FROM "{table}" LIMIT 1'  # nosec B608 - table is an internal allowlist.
        return conn.execute(query).fetchone() is not None

    def mark_onboarding_complete(self) -> bool:
        """Mark onboarding as completed.

        Returns True only when the completion flag actually persisted.
        ``SettingsManager.set`` returns False on a failed write, and discarding
        that made a failed save indistinguishable from success — the user was
        told first run was finished and then walked through the whole thing
        again on every subsequent launch, with nothing surfaced anywhere.
        """
        persisted = bool(self.settings_manager.set("onboarding_completed", True))
        # Cosmetic; the completion flag above is what decides whether first run
        # shows again, so its result is the one that matters.
        self.settings_manager.set("onboarding_completed_at", self._get_timestamp())
        if not persisted:
            logger.error("Onboarding completion did not persist; first run will be shown again on next launch")
            return False
        if self._bootstrap_key and not self._bootstrap_ack:
            mark_bootstrap_key_acknowledged()
            self._bootstrap_ack = True
        logger.info("Onboarding completed")
        return True

    def reset_onboarding(self):
        """Reset onboarding (for testing or if user wants to see it again)"""
        self.settings_manager.set("onboarding_completed", False)
        self._current_step = 0
        logger.info("Onboarding reset")

    def get_current_step(self) -> StepConfig:
        """Get current onboarding step"""
        if self._current_step < len(self._steps):
            return self._steps[self._current_step]
        return self._steps[-1]  # Return last step if completed

    def next_step(self) -> StepConfig | None:
        """Move to next step, returns new step or None if complete"""
        self._current_step += 1
        if self._current_step >= len(self._steps):
            self.mark_onboarding_complete()
            return None
        return self.get_current_step()

    def previous_step(self) -> StepConfig | None:
        """Move to previous step, returns step or None if at start"""
        if self._current_step > 0:
            self._current_step -= 1
            return self.get_current_step()
        return None

    def skip_to_step(self, step_id: OnboardingStep) -> StepConfig:
        """Skip to a specific step"""
        for i, step in enumerate(self._steps):
            if step.id == step_id:
                self._current_step = i
                return step
        return self.get_current_step()

    def get_progress(self) -> dict[str, Any]:
        """Get onboarding progress info"""
        return {
            "current_step": self._current_step,
            "total_steps": len(self._steps),
            "progress_percentage": (self._current_step / len(self._steps)) * 100,
            "is_complete": self.is_onboarding_complete(),
        }

    def save_step_data(self, step_id: OnboardingStep, data: dict[str, Any]):
        """Save user choices/data from a step"""
        key = f"onboarding_step_{step_id.value}"
        self.settings_manager.set(key, data)

    def get_step_data(self, step_id: OnboardingStep) -> dict[str, Any] | None:
        """Get saved data from a step"""
        key = f"onboarding_step_{step_id.value}"
        return self.settings_manager.get(key)

    def _get_timestamp(self) -> str:
        """Get current timestamp"""
        from datetime import datetime

        return datetime.utcnow().isoformat()

    def to_dict(self, step: StepConfig) -> dict[str, Any]:
        """Convert step config to dict for JSON"""
        return {
            "id": step.id.value,
            "title": step.title,
            "description": step.description,
            "content": step.content,
            "icon": step.icon,
            "primary_action": step.primary_action,
            "secondary_action": step.secondary_action,
            "can_skip": step.can_skip,
            "validation_callback": step.validation_callback,
        }


def get_onboarding_system(settings_manager) -> OnboardingSystem:
    """Create onboarding system instance"""
    return OnboardingSystem(settings_manager)


# Probe shape for the advisory Windows mic check below. Several short reads
# rather than one: the first buffer off a freshly opened stream is routinely
# all-zero while capture ramps up, so a single-buffer silence test would call a
# working microphone "blocked".
_MIC_PROBE_FRAMES = 1024
_MIC_PROBE_READS = 6
# PortAudio host APIs disagree about which rates an input device accepts (WASAPI
# in particular rejects 16 kHz outright on devices that work fine in Chromium,
# which resamples). Trying a device's common rates keeps a host-API quirk from
# being reported as a user-facing permission problem.
_MIC_PROBE_RATES = (16000, 48000, 44100)

MIC_GRANTED = "granted"
MIC_BLOCKED = "blocked"
MIC_UNKNOWN = "unknown"


def check_mic_permission_windows() -> tuple[str, str]:
    """Advisory OS-level microphone probe for Windows.

    This is NOT the gate that decides whether first run may continue. Real
    capture happens in Chromium's ``getUserMedia`` inside QtWebEngine
    (``ui/qt_native/webview_window.py`` pre-grants MediaAudioCapture for the
    local origin), which enumerates devices and negotiates rates differently
    from PortAudio. A PortAudio failure therefore proves nothing about whether
    the user can actually talk to Viola, so this function's job is to produce
    *guidance* for a capture failure the frontend already observed — never to
    wall anyone off on its own.

    Returns:
        ``(state, message)`` where state is one of ``"granted"``, ``"blocked"``
        (the OS is delivering digital silence, the signature of a privacy
        denial) or ``"unknown"`` (this stack could not tell either way).
    """
    import sys

    if sys.platform != "win32":
        return (
            MIC_UNKNOWN,
            "Microphone permission can only be confirmed in the Windows desktop app.",
        )

    try:
        import pyaudio  # type: ignore[import-untyped] # AUDIO-01: pyaudio ships no type stubs.

        # audio_core is a desktop-only top-level package (the audio stack) that is
        # deliberately NOT shipped in the cloud image (Dockerfile.cloud does not COPY
        # it). Import portaudio_instance lazily here — inside this Windows-only,
        # ImportError-guarded mic-permission check — so importing ui.onboarding (and
        # everything that transitively imports it: ui.ux_manager -> ui.api.context ->
        # the calendar/feedback/suggestions cloud route groups) never drags audio_core
        # into the cloud process. This function never runs on cloud (returns early on
        # non-win32) and on desktop it works exactly as before.
        from audio_core.portaudio_guard import open_stream, portaudio_instance

        # portaudio_instance() serializes Pa_Initialize/Pa_Terminate under the
        # process-wide lock and terminates on context exit (replacing the finally).
        # open_stream() (not a raw pa.open()) so the probe stream carries its own
        # lock and cannot be freed mid-read by a concurrent teardown (#4650).
        with portaudio_instance() as pa:
            stream = None
            for rate in _MIC_PROBE_RATES:
                try:
                    stream = open_stream(
                        pa,
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=rate,
                        input=True,
                        frames_per_buffer=_MIC_PROBE_FRAMES,
                    )
                    break
                except OSError as exc:
                    logger.debug("Mic probe could not open input at %d Hz: %s", rate, exc)

            if stream is None:
                # PortAudio could not open the device at any rate. Chromium may
                # still capture from it perfectly well, so this is explicitly
                # NOT a denial \u2014 saying "blocked" here is what used to lock
                # people out of first run on a mic that worked.
                return (
                    MIC_UNKNOWN,
                    "Viola could not open the microphone from the desktop audio stack. "
                    "This often still works in the app itself.",
                )

            try:
                for _ in range(_MIC_PROBE_READS):
                    data = stream.read(_MIC_PROBE_FRAMES, exception_on_overflow=False)
                    # A privacy-denied microphone on Windows does not fail the
                    # read and does not return a short buffer \u2014 it returns a
                    # full buffer of digital silence. Testing ``len(data)`` (the
                    # previous check) therefore passed a denied mic as granted.
                    # Any non-zero byte is proof real audio is flowing.
                    if data and any(data):
                        return (MIC_GRANTED, "")
            finally:
                stream.stop_stream()
                stream.close()

            return (
                MIC_BLOCKED,
                "Your microphone is blocked by Windows settings. "
                "Go to Settings \u2192 Privacy \u2192 Microphone to enable it.",
            )
    except ImportError:
        logger.debug("pyaudio not available; microphone permission cannot be confirmed")
        return (
            MIC_UNKNOWN,
            "Viola cannot confirm microphone access from the desktop audio stack.",
        )
    except Exception as exc:
        logger.debug("Mic permission probe was inconclusive: %s", exc)
        return (
            MIC_UNKNOWN,
            "Viola could not confirm microphone access. Check your microphone privacy settings and try again.",
        )


# Pre-built tutorial tips for different features
TUTORIAL_TIPS = [
    TutorialTip(
        command="play [song name]",
        description="Search and play any song from YouTube",
        icon="🎵",
        category="music",
    ),
    TutorialTip(
        command="pause / resume",
        description="Control playback",
        icon="⏯️",
        category="control",
    ),
    TutorialTip(
        command="next / previous",
        description="Skip through your queue",
        icon="⏭️",
        category="control",
    ),
    TutorialTip(
        command="volume [0-100]",
        description="Set volume level",
        icon="🔊",
        category="control",
    ),
    TutorialTip(
        command="what's playing?",
        description="Get current song info",
        icon="❓",
        category="questions",
    ),
    TutorialTip(
        command="play my [playlist name] playlist",
        description="Play from your saved playlists",
        icon="📝",
        category="music",
    ),
    TutorialTip(
        command="what's the weather?",
        description="Get current weather",
        icon="🌤️",
        category="questions",
    ),
    TutorialTip(
        command="tell me a joke",
        description="Have a conversation (requires AI)",
        icon="😄",
        category="questions",
    ),
]
