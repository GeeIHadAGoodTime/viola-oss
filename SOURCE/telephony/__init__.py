"""Telephony package — Pipecat-based AI phone calls.

Uses Pipecat framework with Telnyx for outbound calls,
local faster-whisper for STT, gpt-5.4-mini for LLM,
and local Kokoro for TTS.

Architecture:
    User request → intent tool → CallManager → Pipecat pipeline → Telnyx call

Cost: ~$0.01/minute (Telnyx $0.007/min + 5.4-mini ~$0.004/min).
Latency: ~1.0-2.0s round-trip (VAD + STT + LLM + TTS + transport).

Import note (#1607): the names below are re-exported LAZILY via module
``__getattr__`` (PEP 562), not imported eagerly at package-import time.
Eagerly importing ``telephony.call_manager`` drags in a heavy chain
(Pipecat, faster-whisper, and — via ``telephony.number_validation`` ->
``intent.tools.toll_fraud_prefixes`` -> ``intent/tools/__init__.py`` ->
``intent/tools/desktop.py`` -> ``pyautogui``/``mouseinfo`` — an optional
desktop-automation dependency that requires tkinter). Every real caller in
this codebase already imports the specific submodule directly (e.g.
``from telephony.call_manager import CallManager`` or
``from telephony import call_manager``), so nothing relies on this
package's ``__init__`` having eagerly loaded ``CallManager`` /
``TelnyxConfig`` / ``TelnyxTransport`` as a side effect of merely doing
``import telephony`` (confirmed by a repo-wide search before this change —
no ``from telephony import CallManager`` / ``telephony.CallManager``
usage anywhere). That eager side effect is exactly what made
``core.logging_config._payment_logging_suppressed()`` (which only needs
the tiny, dependency-free ``telephony.payment_sensitive_segment``
submodule) transitively import the entire chain on the first log call of
every process boot. Making these lazy means ``import telephony`` (an
unavoidable prerequisite of importing ANY submodule, including
``payment_sensitive_segment``) no longer touches ``call_manager``,
``config``, or ``telnyx_transport`` until something actually asks for
``telephony.CallManager`` et al. by attribute.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telephony.call_manager import CallManager, CallRecord, CallStatus
    from telephony.config import TelnyxConfig
    from telephony.telnyx_transport import TelnyxTransport, TelnyxTransportParams

__all__ = [
    "CallManager",
    "CallRecord",
    "CallStatus",
    "TelnyxConfig",
    "TelnyxTransport",
    "TelnyxTransportParams",
]

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "CallManager": ("telephony.call_manager", "CallManager"),
    "CallRecord": ("telephony.call_manager", "CallRecord"),
    "CallStatus": ("telephony.call_manager", "CallStatus"),
    "TelnyxConfig": ("telephony.config", "TelnyxConfig"),
    "TelnyxTransport": ("telephony.telnyx_transport", "TelnyxTransport"),
    "TelnyxTransportParams": ("telephony.telnyx_transport", "TelnyxTransportParams"),
}


def __getattr__(name: str) -> object:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value  # cache on the module so repeat access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRS))
