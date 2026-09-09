"""Context enrichment for screen captures.

Augments a raw :class:`~vision.screen_capture.ScreenContext` with
structured text extracted from the UI accessibility tree, OCR, and the
system clipboard.  The enriched context is what gets sent to the vision
LLM for analysis.

Dependencies ``uiautomation``, ``pytesseract``, and ``pyperclip``/
``win32clipboard`` are all optional -- the enricher gracefully degrades
when any of them is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from PIL import Image as _PILImage

    from vision.screen_capture import ScreenContext

logger = get_logger(__name__)

# Maximum clipboard text length forwarded to the LLM.
_MAX_CLIPBOARD_CHARS = 500

# Minimum char count from UIA before falling back to OCR.
_UIA_MIN_CHARS = 20

# ---------------------------------------------------------------------------
# Optional dependency probes
# ---------------------------------------------------------------------------

try:
    import uiautomation

    _HAS_UIA = True
except ImportError:
    _HAS_UIA = False

try:
    import pytesseract

    _HAS_TESSERACT = True
except ImportError:
    _HAS_TESSERACT = False


# ---------------------------------------------------------------------------
# EnrichedContext dataclass
# ---------------------------------------------------------------------------


@dataclass
class EnrichedContext:
    """A screenshot enriched with textual context for LLM analysis.

    Attributes:
        screenshot_b64: Base64-encoded JPEG of the screenshot.
        window_title: Title of the captured window.
        app_name: Executable name of the owning process.
        url: Browser URL if applicable, otherwise ``None``.
        extracted_text: Text extracted from the screen (UIA or OCR).
        clipboard_text: Current clipboard text (truncated to 500 chars).
        user_question: The user's original question about the screen.
        privacy_filtered: ``True`` once the context has passed through
            :meth:`vision.privacy_filter.PrivacyFilter.enforce_context`.
            Only filtered contexts may be sent to a vision LLM.
    """

    screenshot_b64: str
    window_title: str = ""
    app_name: str = ""
    url: str | None = None
    extracted_text: str | None = None
    clipboard_text: str | None = None
    user_question: str = ""
    privacy_filtered: bool = False


# ---------------------------------------------------------------------------
# ContextEnricher
# ---------------------------------------------------------------------------


class ContextEnricher:
    """Enriches a :class:`ScreenContext` with extracted text and clipboard.

    The enrichment strategy is layered:

    1. Attempt **Windows UI Automation** (``uiautomation`` package) to read
       the accessibility tree of the active window.
    2. If UIA yields fewer than 20 characters, fall back to **Tesseract
       OCR** (``pytesseract``).
    3. Independently, try to read the **system clipboard** text.

    All three sources are optional -- if the required library is absent or
    the extraction fails, that field is simply ``None``.
    """

    def enrich(
        self,
        screen_context: ScreenContext,
        user_question: str,
    ) -> EnrichedContext:
        """Build an :class:`EnrichedContext` from a raw capture.

        Args:
            screen_context: The raw screenshot and metadata.
            user_question: The user's natural-language question.

        Returns:
            An :class:`EnrichedContext` ready for vision LLM analysis.
        """
        screenshot_b64 = screen_context.to_base64_jpeg()

        # --- Text extraction (layered) ---
        extracted_text = self._extract_via_uia(screen_context)
        if not extracted_text or len(extracted_text) < _UIA_MIN_CHARS:
            ocr_text = self._extract_via_ocr(screen_context.image)
            if ocr_text and (not extracted_text or len(ocr_text) > len(extracted_text)):
                extracted_text = ocr_text

        # --- Clipboard (explicit consent required; never harvested silently) ---
        clipboard_text: str | None = None
        if self._clipboard_consent_granted():
            clipboard_text = self._get_clipboard()
            if clipboard_text:
                from vision.privacy_filter import get_privacy_filter

                clipboard_text = get_privacy_filter().redact_text(clipboard_text)

        return EnrichedContext(
            screenshot_b64=screenshot_b64,
            window_title=screen_context.window_title,
            app_name=screen_context.app_name,
            url=screen_context.url,
            extracted_text=extracted_text,
            clipboard_text=clipboard_text,
            user_question=user_question,
        )

    # ------------------------------------------------------------------
    # Clipboard consent (SEC-042: never auto-harvest the clipboard)
    # ------------------------------------------------------------------

    @staticmethod
    def _clipboard_consent_granted() -> bool:
        """Return ``True`` only when the user explicitly consented to
        sharing clipboard text with the vision LLM.

        Reads the ``consent_vision_clipboard`` user setting (default
        ``False``). Any failure to resolve the setting fails closed.
        """
        try:
            from ui.settings_manager import get_settings_manager

            return bool(get_settings_manager().get("consent_vision_clipboard", False))
        except (ImportError, OSError, ValueError, RuntimeError, TypeError, AttributeError, KeyError) as exc:
            # Any settings-resolution failure fails closed (no clipboard).
            logger.debug("Clipboard consent lookup failed; failing closed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Extraction backends
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_via_uia(screen_context: ScreenContext) -> str | None:
        """Extract visible text from the active window via UI Automation.

        Uses the ``uiautomation`` library to walk the accessibility tree
        of the foreground window and concatenate all ``Name`` and ``Value``
        properties.

        Returns:
            Concatenated text, or ``None`` if UIA is unavailable or the
            extraction fails.
        """
        if not _HAS_UIA:
            return None
        try:
            import uiautomation as _uia

            control = _uia.GetFocusedControl()
            if control is None:
                return None

            # Walk up to the top-level window
            window = control
            parent = window.GetParentControl()
            while parent is not None:
                try:
                    next_parent = parent.GetParentControl()
                except Exception:
                    break
                if next_parent is None:
                    break
                window = parent
                parent = next_parent

            # Collect text from the accessibility tree
            texts: list[str] = []
            try:
                for child, _depth in _uia.WalkControl(window, maxDepth=6):
                    name = getattr(child, "Name", "") or ""
                    value = ""
                    try:
                        vp = child.GetValuePattern()
                        if vp:
                            value = vp.Value or ""
                    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, LookupError) as exc:
                        # Controls without a value pattern raise; skip the value.
                        logger.debug("UIA value pattern unavailable for control: %s", exc)
                    line = (name + " " + value).strip()
                    if line and line not in texts:
                        texts.append(line)
            except Exception as exc:
                logger.debug("UIA tree walk failed: %s", exc)

            result = "\n".join(texts)
            if result.strip():
                logger.debug(
                    "UIA extracted %s chars from %s",
                    len(result),
                    screen_context.app_name,
                )
                return result
            return None
        except Exception as exc:
            logger.debug("UIA extraction failed: %s", exc)
            return None

    @staticmethod
    def _extract_via_ocr(image: _PILImage.Image) -> str | None:
        """Extract text from the screenshot image via Tesseract OCR.

        Args:
            image: PIL ``Image`` instance.

        Returns:
            Extracted text, or ``None`` if pytesseract is unavailable or
            OCR yields no text.
        """
        if not _HAS_TESSERACT:
            return None
        try:
            import pytesseract as _tess

            text: str = _tess.image_to_string(image)
            text = text.strip()
            if text:
                logger.debug("OCR extracted %s chars", len(text))
                return text
            return None
        except Exception as exc:
            logger.debug("OCR extraction failed: %s", exc)
            return None

    @staticmethod
    def _get_clipboard() -> str | None:
        """Return the current clipboard text, truncated to 500 chars.

        Tries ``win32clipboard`` first, then ``pyperclip`` as a fallback.

        Returns:
            Clipboard text (at most 500 chars), or ``None`` on failure.
        """
        # Attempt 1: win32clipboard (pywin32)
        try:
            import pywintypes
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                if isinstance(data, str) and data.strip():
                    return data.strip()[:_MAX_CLIPBOARD_CHARS]
            finally:
                win32clipboard.CloseClipboard()
        except ImportError:
            logger.debug("win32clipboard unavailable; trying pyperclip")
        except (pywintypes.error, OSError, ValueError, RuntimeError, TypeError) as exc:
            logger.debug("win32clipboard read failed: %s", exc)

        # Attempt 2: pyperclip (PyperclipException is a RuntimeError)
        try:
            import pyperclip

            data = pyperclip.paste()
            if isinstance(data, str) and data.strip():
                return data.strip()[:_MAX_CLIPBOARD_CHARS]
        except ImportError:
            logger.debug("pyperclip unavailable; no clipboard text")
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            logger.debug("pyperclip read failed: %s", exc)

        return None
