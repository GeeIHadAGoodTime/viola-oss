"""Shared response envelope helpers for HTTP and IPC layers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

try:
    from typing_extensions import TypedDict
except ImportError:
    from typing import TypedDict  # Python 3.12+

from core.json_types import JsonObject, JsonValue, to_json_value


class ResponseError(TypedDict, total=False):
    """Structured error payload shared across transports."""

    code: str
    message: str
    details: JsonObject


class ResponseEnvelope(TypedDict):
    """Canonical response envelope."""

    ok: bool
    error: ResponseError | None
    data: JsonValue


class ResponseContractError(RuntimeError):
    """Raised when a payload violates the canonical response contract."""


def _normalize_error(error: object) -> ResponseError | None:
    if error is None:
        return None
    if isinstance(error, ResponseContractError):
        return {
            "code": type(error).__name__,
            "message": str(error) or type(error).__name__,
        }
    if isinstance(error, Exception):
        normalized: ResponseError = {
            "code": type(error).__name__,
            "message": str(error) or repr(error),
        }
        try:
            from core.exceptions import error_details_for_exception

            details = error_details_for_exception(error)
            if details is not None:
                normalized["details"] = details
        except Exception:
            pass
        return normalized
    if isinstance(error, str):
        return {"code": error, "message": error}
    if isinstance(error, Mapping):
        code = str(error.get("code", "")).strip()
        message = str(error.get("message", "")).strip() or code or "unknown_error"
        normalized: ResponseError = {
            "code": code or "unknown_error",
            "message": message,
        }
        details = error.get("details")
        if isinstance(details, Mapping):
            normalized["details"] = {str(k): to_json_value(v) for k, v in details.items()}
        return normalized
    return {
        "code": "unknown_error",
        "message": str(error),
    }


def success_response(data: JsonValue = None) -> ResponseEnvelope:
    """Create a success envelope."""

    return cast(ResponseEnvelope, {"ok": True, "error": None, "data": data})


def failure_response(
    code: str,
    message: str,
    *,
    details: JsonObject | None = None,
    data: JsonValue = None,
) -> ResponseEnvelope:
    """Create a failure envelope."""

    error: ResponseError = {"code": code, "message": message}
    merged_details = details
    if details is not None:
        try:
            from core.exceptions import merge_error_details

            merged_details = merge_error_details(details=details)
        except Exception:
            merged_details = details
    if merged_details is not None:
        error["details"] = merged_details
    return cast(
        ResponseEnvelope,
        {
            "ok": False,
            "error": error,
            "data": data,
        },
    )


def as_envelope(
    ok: bool,
    *,
    data: JsonValue = None,
    error: object = None,
) -> ResponseEnvelope:
    """Normalise arbitrary payload parts into the canonical envelope."""

    normalized_error = _normalize_error(error)
    if ok and normalized_error is not None:
        raise ResponseContractError("Successful responses must not include an error object.")
    return cast(
        ResponseEnvelope,
        {
            "ok": bool(ok),
            "error": normalized_error,
            "data": data,
        },
    )


def ensure_envelope(payload: object) -> ResponseEnvelope:
    """Coerce an arbitrary payload into an envelope or raise."""

    if isinstance(payload, Mapping):
        ok_val = payload.get("ok")
        error_val = payload.get("error")
        data_val = payload.get("data")
        if ok_val is None and "ok" not in payload:
            raise ResponseContractError("Payload missing 'ok' flag.")
        if "data" not in payload:
            return from_legacy(payload)
        normalized = as_envelope(
            bool(ok_val),
            data=to_json_value(data_val),
            error=error_val,
        )
        return normalized
    raise ResponseContractError("Unable to coerce payload into response envelope.")


def is_envelope(payload: object) -> bool:
    """Return True when payload matches the response envelope contract."""

    if not isinstance(payload, Mapping):
        return False
    if "ok" not in payload or "data" not in payload:
        return False
    ok_val = payload.get("ok")
    if not isinstance(ok_val, bool):
        return False
    error_val = payload.get("error")
    if error_val is not None and not isinstance(error_val, Mapping):
        return False
    return True


def unwrap_data(payload: object) -> JsonValue:
    """Return the data portion or raise when envelope indicates failure."""

    envelope = ensure_envelope(payload)
    if not envelope["ok"]:
        error_details = envelope["error"] or {
            "code": "unknown_error",
            "message": "Unknown error",
        }
        raise ResponseContractError(f"{error_details.get('code', 'error')}: {error_details.get('message', 'failure')}")
    return envelope.get("data")


def status_for_envelope(
    envelope: Mapping[str, object],
    *,
    default_success: int = 200,
    default_failure: int = 500,
) -> int:
    """Derive the HTTP status an envelope is allowed to carry.

    The load-bearing invariant of the false-success class (#2736, #2826, #3013):
    a failure envelope (``ok is False``) must NEVER be emitted with a 2xx status.
    A success envelope maps to ``default_success``; a failure envelope maps to a
    ``>= 400`` status. A handler may steer the exact failure code by placing an
    ``http_status`` hint in ``error.details`` — but the hint is honored ONLY when
    it is itself a non-2xx (4xx/5xx) code. A hint that claims 2xx for a failure is
    the exact laundering this prevents, so it is ignored and ``default_failure``
    wins.
    """

    if bool(envelope.get("ok")):
        return default_success
    error = envelope.get("error")
    if isinstance(error, Mapping):
        details = error.get("details")
        if isinstance(details, Mapping):
            raw = details.get("http_status")
            if isinstance(raw, int) and not isinstance(raw, bool) and 400 <= raw <= 599:
                return raw
    return default_failure


def reconcile_status(provided_status: int, envelope: Mapping[str, object]) -> int:
    """Return the honest HTTP status for *envelope*, given a handler-*provided* one.

    A handler that already set an explicit non-2xx status for a failure keeps it
    (it was honest about the failure). A handler that paired a failure envelope
    with a 2xx status is laundering the failure — that pairing is rewritten to the
    contract-derived failure status. Success envelopes keep whatever status the
    handler chose (2xx or otherwise — that is the handler's call).
    """

    if not bool(envelope.get("ok")) and 200 <= provided_status < 300:
        return status_for_envelope(envelope)
    return provided_status


def describe_violation(payload: object) -> tuple[bool, str | None]:
    """Return (is_valid, error_message) to assist diagnostics."""

    if not isinstance(payload, Mapping):
        return False, "Payload is not a mapping"
    if "ok" not in payload:
        return False, "Missing 'ok' key"
    if not isinstance(payload.get("ok"), bool):
        return False, "'ok' must be boolean"
    if "data" not in payload:
        return False, "Missing 'data' key"
    error_val = payload.get("error")
    if error_val is not None and not isinstance(error_val, Mapping):
        return False, "'error' must be null or mapping"
    return True, None


def serialise_envelope(envelope: ResponseEnvelope) -> str:
    """Serialise envelope for logging/debugging."""

    return json.dumps(
        {
            "ok": envelope["ok"],
            "error": envelope["error"],
            "data": envelope["data"],
        },
        default=str,
    )


def from_legacy(payload: Mapping[str, object]) -> ResponseEnvelope:
    """Convert a legacy payload (``ok``/``error`` with top-level fields) to envelope."""

    if "ok" not in payload:
        raise ResponseContractError("Legacy payload missing 'ok' flag.")
    ok_val = bool(payload.get("ok"))
    data_payload: dict[str, JsonValue] = {
        str(k): to_json_value(v) for k, v in payload.items() if k not in {"ok", "error"}
    }
    error_val = payload.get("error")
    if ok_val:
        return success_response(data_payload)
    if error_val is None:
        return failure_response("unknown_error", "Request failed.", data=data_payload)
    if isinstance(error_val, Mapping):
        code = str(error_val.get("code") or "unknown_error")
        message = str(error_val.get("message") or code)
        details = error_val.get("details")
        return failure_response(
            code,
            message,
            data=data_payload,
            details=details if isinstance(details, Mapping) else None,
        )
    if isinstance(error_val, str):
        return failure_response(error_val, error_val, data=data_payload)
    return failure_response("unknown_error", str(error_val), data=data_payload)


__all__ = [
    "ResponseContractError",
    "ResponseEnvelope",
    "ResponseError",
    "as_envelope",
    "describe_violation",
    "ensure_envelope",
    "failure_response",
    "from_legacy",
    "is_envelope",
    "reconcile_status",
    "serialise_envelope",
    "status_for_envelope",
    "success_response",
    "unwrap_data",
]
