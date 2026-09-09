"""Extract the ``call_control_id`` from a Telnyx dial response.

Why this module exists (#3385 / #2589, live-measured on prod 2026-08-05).

``telnyx_client.calls.dial(...)`` returns a ``CallDialResponse`` whose **only**
field is ``data``; the identifier lives at ``response.data.call_control_id``.
Both dial sites used to read it with::

    call_control_id = str(getattr(dial_response, "call_control_id", "") or "")

``CallDialResponse`` has no such attribute, so that expression evaluates to ``""``
on every call that has ever been placed. The failure is silent -- the call still
dials, and the id is later recovered from the media-stream ``start`` event
(``telnyx_transport.py``) -- so nothing looked broken as long as media connected.

It broke two billing-safety mechanisms on the calls where media never connects:

1. **The carrier-reconciliation safety net (#2796/#3385).**
   ``CallManager.get_record_by_call_control_id`` matches webhooks by comparing
   ``record.telnyx_call_control_id``. Left empty, no ``call.answered`` or
   ``call.hangup`` can ever be matched to the record, so ``handle_call_answered``
   / ``handle_call_hangup`` return ``False`` and ``_correct_settled_ledger_status``
   -- the path that corrects an optimistically-settled ledger row -- is
   unreachable. That is precisely the call class (unanswered / declined /
   dropped-before-connect) the safety net was built for. Measured live on prod
   call ``4ca1c734``: ``call.hangup ... cause=timeout, handled=False``.

2. **The conference owner-leg teardown (#2800).** ``call_tools`` records the
   owner leg's control id so every primary-call teardown path hangs it up in
   lockstep. Empty, the leg is never registered and keeps billing until
   ``time_limit_secs`` expires.

Capturing the id at dial time -- before any media -- is what makes both
mechanisms reachable, so the extraction is shared here and gated by
``scripts/check_telnyx_dial_control_id_capture.py``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["telnyx_dial_call_control_id"]


def _coerce(value: Any) -> str:
    """Return *value* as a stripped string, treating None/non-scalars as empty."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _read(source: Any, key: str) -> Any:
    """Read *key* from *source* whether it is a mapping or an object."""
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def telnyx_dial_call_control_id(dial_response: Any) -> str:
    """Return the ``call_control_id`` carried by a Telnyx dial response.

    Handles the shape the SDK actually returns (``response.data.call_control_id``)
    and stays tolerant of the flat and mapping shapes so an SDK upgrade that moves
    the field cannot silently reintroduce the empty-id bug. Returns ``""`` when the
    response genuinely carries no id -- callers must keep treating empty as "not
    known yet" and fall back to the media-stream capture.
    """
    if dial_response is None:
        return ""

    # The real SDK shape first: CallDialResponse.data.call_control_id.
    data = _read(dial_response, "data")
    if data is not None:
        nested = _coerce(_read(data, "call_control_id"))
        if nested:
            return nested

    # Flat shape (older/alternate SDKs, and hand-rolled fakes).
    return _coerce(_read(dial_response, "call_control_id"))
