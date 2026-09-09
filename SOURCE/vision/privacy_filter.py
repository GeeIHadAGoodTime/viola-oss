"""Privacy filter for screen awareness.

Prevents sensitive information from being sent to the vision LLM by:

* **Blocking** captures of password managers and other sensitive apps.
* **Redacting** credit card numbers, SSNs, API keys, tokens, and other
  secrets from extracted text before it reaches the model.
* **Assessing risk** of a given :class:`~vision.screen_capture.ScreenContext`
  so callers can decide whether to proceed, warn, or abort.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from PIL import Image as _PILImage

    from vision.context_enrichment import EnrichedContext
    from vision.screen_capture import ScreenContext

logger = get_logger(__name__)


class PrivacyBlockedError(ValueError):
    """Raised when screen content must not be sent to the vision LLM.

    ``str(exc)`` is always the stable token ``"vision_privacy_blocked"`` so
    transport layers can map it without parsing prose; the human-readable
    explanation lives in :attr:`reason` (logged, never sent to the model).
    """

    def __init__(self, reason: str) -> None:
        super().__init__("vision_privacy_blocked")
        self.reason = reason


# ---------------------------------------------------------------------------
# Sensitive patterns
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Credit card numbers (4-4-4-4 or 16 contiguous digits)
    (
        re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),
        "[REDACTED_CREDIT_CARD]",
    ),
    # US Social Security Numbers (3-2-4)
    (
        re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    # Generic API keys / secrets (long hex or base64 tokens prefixed by
    # common variable names)
    (
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|password|passwd|pwd)" r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-/.+=]{16,})['\"]?"
        ),
        "[REDACTED_SECRET]",
    ),
    # Bearer tokens
    (
        re.compile(r"(?i)Bearer\s+[A-Za-z0-9_\-/.+=]{16,}"),
        "[REDACTED_BEARER_TOKEN]",
    ),
    # AWS-style access key IDs (AKIA...)
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    # Generic long hex strings (>= 32 chars -- likely keys or hashes)
    (
        re.compile(r"\b[0-9a-fA-F]{32,}\b"),
        "[REDACTED_HEX_SECRET]",
    ),
    # Private key headers
    (
        re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Password-field indicators in accessibility text
    (
        re.compile(r"(?i)(?:password|passphrase|pin)\s*[:=]\s*\S+"),
        "[REDACTED_PASSWORD]",
    ),
]

# ---------------------------------------------------------------------------
# Sensitive applications
# ---------------------------------------------------------------------------

_SENSITIVE_APPS: frozenset[str] = frozenset(
    {
        "1password",
        "1password.exe",
        "lastpass",
        "lastpass.exe",
        "bitwarden",
        "bitwarden.exe",
        "keepass",
        "keepass.exe",
        "keepassxc",
        "keepassxc.exe",
        "keychain access",
        "credential manager",
        "dashlane",
        "dashlane.exe",
        "nordpass",
        "nordpass.exe",
        "enpass",
        "enpass.exe",
        "roboform",
        "roboform.exe",
    }
)

# Title-bar keywords that indicate a sensitive context even in non-password
# manager apps (e.g. a browser on a login page).
_SENSITIVE_TITLE_KEYWORDS: frozenset[str] = frozenset(
    {
        "password manager",
        "vault",
        "keychain",
        "credentials",
        "master password",
        "secret key",
    }
)

# ---------------------------------------------------------------------------
# Sensitive URL paths
# ---------------------------------------------------------------------------

_SENSITIVE_URL_PATHS: list[re.Pattern[str]] = [
    re.compile(r"/log[-_]?in", re.IGNORECASE),
    re.compile(r"/sign[-_]?in", re.IGNORECASE),
    re.compile(r"/auth", re.IGNORECASE),
    re.compile(r"/oauth", re.IGNORECASE),
    re.compile(r"/account", re.IGNORECASE),
    re.compile(r"/password", re.IGNORECASE),
    re.compile(r"/2fa", re.IGNORECASE),
    re.compile(r"/mfa", re.IGNORECASE),
    re.compile(r"/security", re.IGNORECASE),
    re.compile(r"/checkout", re.IGNORECASE),
    re.compile(r"/payment", re.IGNORECASE),
    re.compile(r"/billing", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# PrivacyFilter
# ---------------------------------------------------------------------------


class PrivacyFilter:
    """Gate-keeper that blocks, redacts, or warns about sensitive screen content.

    Typical usage::

        pf = PrivacyFilter()

        # Before capturing
        if pf.should_block(window_title, app_name):
            return "I won't look at password managers."

        # After capturing
        level, reason = pf.assess_risk(screen_context)
        if level == "blocked":
            return reason

        # Before sending to LLM
        safe_text = pf.redact_text(raw_extracted_text)
    """

    # Expose class-level data for testing / extension
    _SENSITIVE_PATTERNS = _SENSITIVE_PATTERNS
    _SENSITIVE_APPS = _SENSITIVE_APPS

    # ------------------------------------------------------------------
    # Blocking
    # ------------------------------------------------------------------

    def should_block(self, window_title: str, app_name: str) -> bool:
        """Return ``True`` if the app or window is categorically sensitive.

        This check should be performed *before* capturing a screenshot so
        that no image data of a password manager is ever created.

        Args:
            window_title: The title text of the window.
            app_name: The executable or process name.

        Returns:
            ``True`` if the capture should be blocked outright.
        """
        # Check app name
        if app_name.lower() in self._SENSITIVE_APPS:
            logger.info("Blocking screen capture for sensitive app: %s", app_name)
            return True

        # Check title keywords
        title_lower = window_title.lower()
        for keyword in _SENSITIVE_TITLE_KEYWORDS:
            if keyword in title_lower:
                logger.info(
                    "Blocking screen capture due to sensitive title keyword: %s",
                    keyword,
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        """Replace sensitive patterns in *text* with redaction placeholders.

        Args:
            text: Raw text to sanitise.

        Returns:
            The text with all recognised sensitive patterns replaced.
        """
        if not text:
            return text

        result = text
        for pattern, replacement in self._SENSITIVE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    # ------------------------------------------------------------------
    # Image-level redaction
    # ------------------------------------------------------------------

    def redact_image(self, image: _PILImage.Image) -> tuple[_PILImage.Image, int] | None:
        """Black out OCR lines in *image* that contain sensitive patterns.

        Runs Tesseract OCR with word bounding boxes, reconstructs each text
        line, and masks the full line region whenever
        :meth:`redact_text` would alter it.

        Args:
            image: PIL image of the screen capture.

        Returns:
            ``(redacted_image, masked_line_count)`` on success, or ``None``
            when OCR is unavailable (library or binary missing) -- callers
            must fail closed on other sensitivity signals in that case.
        """
        try:
            import pytesseract as _tess
        except ImportError:
            return None

        try:
            data = _tess.image_to_data(image, output_type=_tess.Output.DICT)
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            # TesseractNotFoundError is an OSError; TesseractError is a
            # RuntimeError.  OCR failure degrades to None so callers fail
            # closed on any sensitivity signal.
            logger.warning("Image redaction OCR pass unavailable: %s", exc)
            return None

        lines: dict[tuple[int, int, int], list[int]] = {}
        words = data.get("text", [])
        for idx, word in enumerate(words):
            if not str(word).strip():
                continue
            key = (
                int(data["block_num"][idx]),
                int(data["par_num"][idx]),
                int(data["line_num"][idx]),
            )
            lines.setdefault(key, []).append(idx)

        masked = 0
        result = image
        draw = None
        for indices in lines.values():
            line_text = " ".join(str(words[i]) for i in indices)
            if self.redact_text(line_text) == line_text:
                continue
            if draw is None:
                from PIL import ImageDraw

                result = image.copy()
                draw = ImageDraw.Draw(result)
            left = min(int(data["left"][i]) for i in indices)
            top = min(int(data["top"][i]) for i in indices)
            right = max(int(data["left"][i]) + int(data["width"][i]) for i in indices)
            bottom = max(int(data["top"][i]) + int(data["height"][i]) for i in indices)
            draw.rectangle((left, top, right, bottom), fill="black")
            masked += 1

        if masked:
            logger.info("Image redaction masked %s sensitive line(s) before vision LLM egress", masked)
        return result, masked

    # ------------------------------------------------------------------
    # Egress enforcement (the production chokepoint)
    # ------------------------------------------------------------------

    def enforce_context(self, context: EnrichedContext) -> EnrichedContext:
        """Block or scrub *context* before it may leave the device for an LLM.

        This is the mandatory gate on every vision-LLM egress path. It:

        1. Hard-blocks sensitive apps/window titles (:meth:`should_block`).
        2. Redacts sensitive patterns from extracted text and clipboard text.
        3. Masks sensitive pixel regions via :meth:`redact_image`; when the
           pixels cannot be scrubbed (no OCR) or a detected sensitive text
           region cannot be located in the image, the frame is blocked
           (fail closed) rather than sent raw.

        Args:
            context: The enriched screen context about to be sent.

        Returns:
            A filtered copy with ``privacy_filtered=True``. Already-filtered
            contexts pass through unchanged (idempotent).

        Raises:
            PrivacyBlockedError: when the capture must not be sent at all.
        """
        if getattr(context, "privacy_filtered", False):
            return context

        from dataclasses import replace

        if self.should_block(context.window_title or "", context.app_name or ""):
            raise PrivacyBlockedError(
                "Sensitive application or window title detected (%s)." % (context.app_name or context.window_title)
            )

        risk_level, _risk_reason = self.assess_risk(context)
        if risk_level == "blocked":
            raise PrivacyBlockedError("Screen capture risk assessment returned blocked.")

        extracted = self.redact_text(context.extracted_text) if context.extracted_text else context.extracted_text
        clipboard = self.redact_text(context.clipboard_text) if context.clipboard_text else context.clipboard_text
        extracted_was_sensitive = extracted != context.extracted_text

        screenshot_b64 = context.screenshot_b64
        if screenshot_b64:
            screenshot_b64 = self._scrub_screenshot(
                screenshot_b64,
                extracted_was_sensitive=extracted_was_sensitive,
                risk_level=risk_level,
            )

        return replace(
            context,
            screenshot_b64=screenshot_b64,
            extracted_text=extracted,
            clipboard_text=clipboard,
            privacy_filtered=True,
        )

    def _scrub_screenshot(
        self,
        screenshot_b64: str,
        *,
        extracted_was_sensitive: bool,
        risk_level: str,
    ) -> str:
        """Redact the base64 screenshot pixels, failing closed when impossible.

        Args:
            screenshot_b64: Base64-encoded screenshot about to egress.
            extracted_was_sensitive: Whether text redaction fired on the
                extracted-text channel for this capture.
            risk_level: The :meth:`assess_risk` level for this capture.

        Returns:
            Base64 JPEG with sensitive regions masked (or the original when
            nothing needed masking and the image is provably scannable).

        Raises:
            PrivacyBlockedError: when pixels cannot be decoded or cannot be
                scrubbed while sensitivity signals are present.
        """
        try:
            from PIL import Image

            raw = base64.b64decode(screenshot_b64)
            with Image.open(io.BytesIO(raw)) as source:
                image = source.convert("RGB")
        except (OSError, ValueError, binascii.Error, ImportError) as exc:
            # Un-inspectable pixels never egress.
            raise PrivacyBlockedError("Screenshot could not be decoded for privacy scrubbing.") from exc

        result = self.redact_image(image)
        if result is None:
            if extracted_was_sensitive or risk_level != "safe":
                raise PrivacyBlockedError(
                    "Sensitive content detected but image redaction is unavailable; refusing to send raw pixels."
                )
            logger.warning(
                "Image redaction unavailable (OCR missing); frame sent without pixel scrubbing because no sensitivity signals were detected"
            )
            return screenshot_b64

        redacted_image, masked = result
        if extracted_was_sensitive and masked == 0:
            # Text channel says secrets are present but we could not locate
            # them in the pixels -- we cannot prove the image is clean.
            raise PrivacyBlockedError(
                "Sensitive text detected but no matching image region could be masked; refusing to send raw pixels."
            )
        if masked == 0:
            return screenshot_b64

        output = io.BytesIO()
        redacted_image.save(output, format="JPEG", quality=85)
        return base64.b64encode(output.getvalue()).decode("ascii")

    # ------------------------------------------------------------------
    # Risk assessment
    # ------------------------------------------------------------------

    def assess_risk(self, screen_context: ScreenContext | EnrichedContext) -> tuple[str, str]:
        """Assess the privacy risk of a captured :class:`ScreenContext`.

        Returns a tuple of ``(risk_level, reason)`` where *risk_level* is
        one of:

        * ``"blocked"`` -- capture must not be sent to an LLM.
        * ``"caution"`` -- capture may contain sensitive content; proceed
          with redaction and user warning.
        * ``"safe"`` -- no sensitive signals detected.

        Args:
            screen_context: The screen capture to assess.

        Returns:
            ``(risk_level, reason)`` tuple.
        """
        # Hard block
        if self.should_block(screen_context.window_title, screen_context.app_name):
            return (
                "blocked",
                "Screen capture blocked: sensitive application detected (%s)." % screen_context.app_name,
            )

        # URL sensitivity
        if screen_context.url and self._check_url_sensitivity(screen_context.url):
            return (
                "caution",
                "The current page appears to be a login or payment page.  " "Sensitive fields will be redacted.",
            )

        return ("safe", "")

    # ------------------------------------------------------------------
    # URL sensitivity check
    # ------------------------------------------------------------------

    @staticmethod
    def _check_url_sensitivity(url: str) -> bool:
        """Return ``True`` if the URL path matches known sensitive paths.

        Args:
            url: The URL to inspect.

        Returns:
            ``True`` when the URL contains login, auth, payment, or
            similar paths.
        """
        for pattern in _SENSITIVE_URL_PATHS:
            if pattern.search(url):
                return True
        return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_privacy_filter: PrivacyFilter | None = None


def get_privacy_filter() -> PrivacyFilter:
    """Return the process-wide :class:`PrivacyFilter` instance."""
    global _privacy_filter
    if _privacy_filter is None:
        _privacy_filter = PrivacyFilter()
    return _privacy_filter
