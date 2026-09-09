"""SEC-08: central enforcement of user consent on OpenAI API calls.

Every production OpenAI call site (responses, batches, audio, embeddings,
files, and explicit third-party chat-compatible branches) must pass through
``enforce_storage_consent``
before invoking the SDK.  The helper sets ``store=False`` whenever the
user has not consented to server-side retention, overriding any caller-
provided value — including the `responses.create` default of `True`.

Fail-closed: any failure to read the consent flag (e.g. SettingsManager
unavailable, env not loaded) forces ``store=False``.

Usage:

    from services.llm.openai_consent import enforce_storage_consent

    api_kwargs = {...}
    api_kwargs = enforce_storage_consent(api_kwargs)
    response = await client.responses.create(**api_kwargs)
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_store_override_warning_logged = False


def enforce_storage_consent(api_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mutate ``api_kwargs`` so ``store`` respects the user's consent flag.

    Behaviour:
      * If ``is_openai_storage_consented()`` returns True, the caller's
        ``store`` value (if any) is left untouched.
      * Otherwise ``api_kwargs["store"] = False`` is set, overriding any
        caller-provided ``True`` with a WARNING log.
      * If the consent flag cannot be read, fail-closed: set False.

    The function mutates and returns ``api_kwargs`` for convenience so
    call sites can inline it: ``await client.responses.create(
    **enforce_storage_consent(api_kwargs))``.
    """
    try:
        from core.privacy_consent import is_openai_storage_consented

        consented = bool(is_openai_storage_consented())
    except Exception:
        logger.exception("SEC-08: consent read failed; forcing store=False")
        consented = False

    if not consented:
        requested = api_kwargs.get("store")
        if requested is True:
            global _store_override_warning_logged
            log_fn = logger.debug if _store_override_warning_logged else logger.warning
            log_fn("SEC-08: caller requested store=True but user has not consented; forcing store=False")
            _store_override_warning_logged = True
        api_kwargs["store"] = False
    return api_kwargs


__all__ = ["enforce_storage_consent"]
