from __future__ import annotations

from core.console import console

"""
Centralised logging bootstrap with structured context helpers.

All runtime modules should import ``get_logger`` from here instead of touching
Loguru or configuring stdlib logging on their own.  The helper ensures that:

* Observability sinks are initialised once per process
* Loggers are namespaced under the ``viola`` root for easier filtering
* Keyword arguments on logging calls become structured fields automatically
"""

import logging
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, TextIO, TypedDict, cast


class SupportsStr(Protocol):
    def __str__(self, /) -> str: ...


LogValue = SupportsStr | type[object]


ExcInfoValue = (
    bool
    | tuple[type[BaseException], BaseException, TracebackType | None]
    | tuple[None, None, None]
    | BaseException
    | None
)


class _PassthroughLogKwargs(TypedDict, total=False):
    exc_info: ExcInfoValue
    stack_info: bool
    stacklevel: int
    extra: Mapping[str, LogValue]


_ROOT_LOGGER_NAME = "viola"
_STD_LOG_KWARGS = {"exc_info", "stack_info", "stacklevel", "extra"}
_LOGGER_CACHE: dict[str, StructuredLogger] = {}
_LOGGING_READY = False
_HANDLER_ID_COUNTER = 0
_HANDLER_REGISTRY: dict[int, logging.Handler] = {}


def _next_handler_id() -> int:
    global _HANDLER_ID_COUNTER
    _HANDLER_ID_COUNTER += 1
    return _HANDLER_ID_COUNTER


class _CallableSinkHandler(logging.Handler):
    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self._sink(message)
        except Exception:
            return


def _setup_basic_logging_utf8() -> None:
    """Configure stdlib root logger with a UTF-8-safe console handler.

    Replacement for ``logging.basicConfig()`` that explicitly reconfigures
    ``sys.stderr`` to UTF-8 before attaching the StreamHandler.  On Windows
    the default stderr encoding is cp1252; log messages containing CJK
    characters or emoji would otherwise raise ``UnicodeEncodeError`` and
    crash the process.
    """
    import sys as _sys

    stderr = _sys.stderr
    _reconfigure = getattr(stderr, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            logging.getLogger(__name__).debug("stderr UTF-8 reconfigure failed")

    handler = logging.StreamHandler(stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def _safe_configure_observability() -> None:
    """
    Configure observability sinks if they haven't been configured already.

    This function ensures observability is set up but doesn't return the config
    since the type is external and not guaranteed to be available.
    """

    global _LOGGING_READY

    if _LOGGING_READY:
        return None

    configure_observability = None
    get_observability_config = None
    try:  # pragma: no cover - optional in minimal environments
        # Known circular import: config.settings → core.logging_config →
        # diagnostics.observability_logging → config.settings.
        # This fails on first import (settings partially initialized).
        # The except clause falls through to basic logging, which is fine.
        # On subsequent calls (_LOGGING_READY=True), this block is skipped.
        from diagnostics.observability_logging import (
            configure_observability as _configure_observability,
            get_observability_config as _get_observability_config,
        )

        configure_observability = _configure_observability
        get_observability_config = _get_observability_config
    except Exception:  # pragma: no cover
        # Expected on first import due to circular dependency — not an error.
        pass

    if get_observability_config is not None:
        existing = get_observability_config()
        if existing is not None:
            _LOGGING_READY = True
            return None

    if configure_observability is None:
        if not logging.getLogger().handlers:
            _setup_basic_logging_utf8()
        _LOGGING_READY = True
        return None

    try:
        configure_observability()
        _LOGGING_READY = True
        return None
    except Exception as exc:
        import sys

        console(f"BOOTSTRAP: Failed to configure observability: {exc}", file=sys.stderr)
        if not logging.getLogger().handlers:
            _setup_basic_logging_utf8()
        _LOGGING_READY = True
        return None


def _namespace(name: str | None) -> str:
    if not name:
        return _ROOT_LOGGER_NAME
    if name.startswith(_ROOT_LOGGER_NAME + "."):
        return name
    return f"{_ROOT_LOGGER_NAME}.{name}"


def get_logger(name: str | None = None) -> StructuredLogger:
    """
    Return a structured logger bound to the provided module name.

    Args:
        name: Module name (usually ``__name__``). When omitted the root
            ``viola`` namespace is used.
    """

    qualified_name = _namespace(name)
    if qualified_name in _LOGGER_CACHE:
        return _LOGGER_CACHE[qualified_name]

    _safe_configure_observability()
    logger = StructuredLogger(logging.getLogger(qualified_name))
    _LOGGER_CACHE[qualified_name] = logger
    return logger


def get_child_logger(parent: StructuredLogger, suffix: str) -> StructuredLogger:
    """
    Helper to derive a child logger while preserving existing context.
    """

    qualified = _namespace(f"{parent.name}.{suffix}")
    return StructuredLogger(logging.getLogger(qualified), context=parent.context)


@dataclass(frozen=True)
class StructuredLogger:
    """
    Thin adapter that mirrors Loguru's ergonomics on top of stdlib logging.

    * Keyword args become structured context (just like loguru's ``bind``)
    * ``bind`` returns another ``StructuredLogger`` with merged context
    * Supports the usual ``.debug/.info/.warning/.error/.exception`` helpers
    """

    _logger: logging.Logger
    context: Mapping[str, LogValue] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", dict(self.context or {}))

    # ------------------------------------------------------------------ #
    # Public properties                                                  #
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self._logger.name

    def getChild(self, suffix: str) -> StructuredLogger:
        return get_child_logger(self, suffix)

    @property
    def handlers(self) -> list[logging.Handler]:
        """Proxy to underlying logger's handlers."""
        return self._logger.handlers

    @property
    def level(self) -> int:
        """Proxy to underlying logger's level."""
        return self._logger.level

    # ------------------------------------------------------------------ #
    # Structured logging helpers                                         #
    # ------------------------------------------------------------------ #
    def bind(self, **kwargs: LogValue) -> StructuredLogger:
        merged = dict(self.context or {})
        merged.update(kwargs)
        return StructuredLogger(self._logger, context=merged)

    def with_context(self, **kwargs: LogValue) -> StructuredLogger:
        return self.bind(**kwargs)

    # ------------------------------------------------------------------ #
    # Proxy methods                                                      #
    # ------------------------------------------------------------------ #
    def debug(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        self._log(logging.CRITICAL, msg, *args, **kwargs)

    def exception(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        kwargs.setdefault("exc_info", True)
        self._log(logging.ERROR, msg, *args, **kwargs)

    def log(self, level: int, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        self._log(level, msg, *args, **kwargs)

    def success(self, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        """Log a success message at INFO level (Loguru compatibility)."""
        self._log(logging.INFO, msg, *args, **kwargs)

    def add(
        self,
        sink: Callable[[str], None] | str | Path | TextIO,
        *,
        level: int | str = logging.DEBUG,
    ) -> int:
        """
        Minimal Loguru-style sink support.

        Supports:
        - `logger.add(callable, level="INFO")` where callable receives a formatted string
        - `logger.add("path/to/file.log", level="INFO")`
        """
        resolved_level = logging.getLevelName(level) if isinstance(level, int) else level
        numeric_level = logging.getLevelName(resolved_level)
        if not isinstance(numeric_level, int):
            numeric_level = logging.DEBUG

        handler: logging.Handler
        if callable(sink):
            handler = _CallableSinkHandler(sink)
        elif isinstance(sink, (str, Path)):
            handler = logging.FileHandler(str(sink), encoding="utf-8")
        else:
            stream = sink

            def _write(message: str) -> None:
                stream.write(f"{message}\n")
                try:
                    stream.flush()
                except Exception:
                    return

            handler = _CallableSinkHandler(_write)

        handler.setLevel(numeric_level)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger(_ROOT_LOGGER_NAME).addHandler(handler)

        handler_id = _next_handler_id()
        _HANDLER_REGISTRY[handler_id] = handler
        return handler_id

    def remove(self, handler_id: int | None = None) -> None:
        """Remove a handler previously added via `add()`."""
        if handler_id is None:
            for hid in list(_HANDLER_REGISTRY.keys()):
                self.remove(hid)
            return

        handler = _HANDLER_REGISTRY.pop(handler_id, None)
        if handler is None:
            return
        logging.getLogger(_ROOT_LOGGER_NAME).removeHandler(handler)
        try:
            handler.close()
        except Exception:
            return

    def configure(self, *args: LogValue, **kwargs: LogValue) -> None:
        """Stub for Loguru configure compatibility."""
        pass

    def setLevel(self, level: int | str) -> None:
        """Proxy to underlying logger's setLevel."""
        self._logger.setLevel(level)

    def addHandler(self, handler: logging.Handler) -> None:
        """Proxy to underlying logger's addHandler."""
        self._logger.addHandler(handler)

    def removeHandler(self, handler: logging.Handler) -> None:
        """Proxy to underlying logger's removeHandler."""
        self._logger.removeHandler(handler)

    # ------------------------------------------------------------------ #
    # Internal plumbing                                                  #
    # ------------------------------------------------------------------ #
    def _log(self, level: int, msg: str, *args: LogValue, **kwargs: LogValue) -> None:
        if not self._logger.isEnabledFor(level):
            return
        if level < logging.WARNING and _payment_logging_suppressed():
            return
        passthrough, structured = _split_log_kwargs(kwargs)
        if args:
            msg, args = _redact_formatted_log_message(msg, args)
        msg = str(_redact_log_value(msg))
        args = cast(tuple[LogValue, ...], tuple(_redact_log_value(arg) for arg in args))

        if structured:
            structured = cast(dict[str, LogValue], _redact_log_value(dict(structured)))
        if passthrough.get("extra"):
            passthrough["extra"] = cast(Mapping[str, LogValue], _redact_log_value(dict(passthrough["extra"])))

        extra = _merge_contexts(self.context, structured, passthrough.get("extra"))
        if extra:
            extra = cast(dict[str, LogValue], _redact_log_value(extra))
        if extra:
            passthrough["extra"] = extra
        self._logger.log(level, msg, *args, **passthrough)


def _split_log_kwargs(
    incoming: MutableMapping[str, LogValue],
) -> tuple[_PassthroughLogKwargs, dict[str, LogValue]]:
    passthrough: _PassthroughLogKwargs = {}
    structured: dict[str, LogValue] = {}

    for key, value in list(incoming.items()):
        if key in _STD_LOG_KWARGS:
            if key == "extra":
                passthrough["extra"] = cast(Mapping[str, LogValue], value)
            elif key == "exc_info":
                passthrough["exc_info"] = cast(ExcInfoValue, value)
            elif key == "stack_info":
                passthrough["stack_info"] = cast(bool, value)
            elif key == "stacklevel":
                passthrough["stacklevel"] = cast(int, value)
        else:
            structured[key] = value
        incoming.pop(key, None)

    return passthrough, structured


def _merge_contexts(*contexts: Mapping[str, LogValue] | MutableMapping[str, LogValue] | None) -> dict[str, LogValue]:
    merged: dict[str, LogValue] = {}
    for ctx in contexts:
        if not ctx:
            continue
        if isinstance(ctx, dict):
            merged.update(ctx)
        else:
            merged.update(dict(ctx))
    return merged


def _redact_log_value(value: object) -> object:
    value = _redact_diagnostic_log_value(value)
    return _mask_secret_log_value(value)


def _redact_diagnostic_log_value(value: object) -> object:
    try:
        from intent.log_redaction import redact_diagnostic_payload
    except ImportError:
        return value
    return redact_diagnostic_payload(value)


def _mask_secret_log_value(value: object) -> object:
    try:
        from core.secrets_mask import mask_dict_secrets, mask_secrets_in_text
    except ImportError:
        return value

    if isinstance(value, Mapping):
        return mask_dict_secrets(dict(value))
    if isinstance(value, str):
        return mask_secrets_in_text(value)
    if isinstance(value, list):
        return [_mask_secret_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_secret_log_value(item) for item in value)
    if isinstance(value, set):
        return {_mask_secret_log_value(item) for item in value}
    return value


_PAYMENT_LOGGING_SUPPRESSED_FN: Callable[[], bool] | None = None
_PAYMENT_LOGGING_SUPPRESSED_RESOLVED = False


def _payment_logging_suppressed() -> bool:
    """Return True while a call is inside a spoken-payment log-suppression window.

    Resolves ``telephony.payment_sensitive_segment.payment_logging_suppressed``
    lazily, but ONLY ONCE per process (cached in the module globals above).

    This runs on every sub-WARNING log call anywhere in the codebase (see
    ``_log`` below), so an unmemoized ``from telephony... import`` here is a
    hot-path landmine: importing the ``telephony`` package eagerly pulls in
    ``telephony.call_manager`` -> pipecat's voicemail/turn-detection stack ->
    ``transformers`` -> a fresh ``import torch``. Any code elsewhere that
    intentionally evicts torch from ``sys.modules`` to free memory (see
    ``violawake.engine._release_torch_if_loaded``) would have its own success
    LOG LINE silently reimport torch right back in via this path, on every
    call — repeated cold reinit of a heavy native extension in one process,
    which is what segfaulted CI (#697). Caching the resolution once removes
    the import statement from the hot path entirely after the first call.
    """
    global _PAYMENT_LOGGING_SUPPRESSED_FN, _PAYMENT_LOGGING_SUPPRESSED_RESOLVED
    if not _PAYMENT_LOGGING_SUPPRESSED_RESOLVED:
        try:
            from telephony.payment_sensitive_segment import payment_logging_suppressed as _resolved_fn
        except Exception:  # noqa: BLE001, RUF100 - resolved once; unavailable means "never suppress", not a crash
            _resolved_fn = None
        _PAYMENT_LOGGING_SUPPRESSED_FN = _resolved_fn
        _PAYMENT_LOGGING_SUPPRESSED_RESOLVED = True
    if _PAYMENT_LOGGING_SUPPRESSED_FN is None:
        return False
    return _PAYMENT_LOGGING_SUPPRESSED_FN()


def _redact_formatted_log_message(msg: str, args: tuple[LogValue, ...]) -> tuple[str, tuple[LogValue, ...]]:
    try:
        rendered = msg % args
    except Exception:
        return msg, args
    redacted = _redact_log_value(rendered)
    if redacted != rendered:
        return str(redacted), ()
    return msg, args


__all__ = [
    "StructuredLogger",
    "get_child_logger",
    "get_logger",
]
