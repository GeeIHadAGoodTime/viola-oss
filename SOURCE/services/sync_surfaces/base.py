"""Shared SQL helpers for one-row and keyed Tier-2 sync surfaces."""

from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import asyncpg

from services.credential_boundary import (
    contains_payment_card_pan as boundary_contains_payment_card_pan,
    is_tier3_credential_key,
    is_tier3_credential_value,
)
from services.sync import (
    IncomingMutation,
    ResolutionOutcome,
    ServerRowSnapshot,
    VersionVector,
    resolve,
    stamp,
    write_journal,
)

JsonDict = dict[str, Any]

SYNC_LIMIT_DEFAULT = 500
SYNC_LIMIT_MAX = 500
MAX_SYNC_DIRECT_DEVICE_ID_LENGTH = 128
MAX_SYNC_DIRECT_FIELD_VERSION_ENTRIES = 256
MAX_SYNC_DIRECT_FIELD_VERSION_NAME_LENGTH = 128
MAX_SYNC_DIRECT_HLC_ACTOR_ID_LENGTH = 128
MAX_SYNC_DIRECT_HLC_COUNTER = 1_000_000
MAX_SYNC_DIRECT_HLC_FUTURE_SKEW_MS = 10 * 60 * 1000
MAX_SYNC_DIRECT_IDENTIFIER_LENGTH = 128
MAX_SYNC_DIRECT_JSON_ARRAY_ITEMS = 1024
MAX_SYNC_DIRECT_JSON_DEPTH = 24
MAX_SYNC_DIRECT_JSON_OBJECT_KEYS = 256
MAX_SYNC_DIRECT_ROW_JSON_BYTES = 64 * 1024
MAX_SYNC_DIRECT_STRING_BYTES = 16 * 1024
MAX_SYNC_DIRECT_VERSION_VECTOR_ACTOR_LENGTH = 128
MAX_SYNC_DIRECT_VERSION_VECTOR_COUNTER = 9_000_000_000_000_000
MAX_SYNC_DIRECT_VERSION_VECTOR_ENTRIES = 64
_MISSING = object()
TOKEN_METADATA_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "token",
        "token_value",
    }
)
PII_DENYLIST_SYNC_SURFACES: frozenset[str] = frozenset({"metadata"})
PII_SYNC_EXACT_KEYS: frozenset[str] = frozenset(
    {
        "contact_email",
        "date_of_birth",
        "dob",
        "email",
        "email_address",
        "home_phone",
        "mailing_address",
        "phone",
        "phone_number",
        "postal_address",
        "social_security",
        "social_security_number",
        "ssn",
        "street_address",
        "tax_id",
    }
)
STRICT_CONFLICT_SYNC_SURFACES: frozenset[str] = frozenset(
    {"action_recipes", "proactive_tasks", "queue_items", "schedules"}
)
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_HLC_RE = re.compile(r"^(?P<ms>\d+):(?P<counter>\d+):(?P<actor>.+)$")
_SCRIPT_TAG_RE = re.compile(r"<\s*/?\s*script\b", re.IGNORECASE)
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_SYNC_MUTATION_REQUIRED_KEYS = frozenset(
    {"version_vector", "lww_hlc", "lww_actor_id", "last_mutation_id", "field_versions"}
)


class SyncSurfaceError(ValueError):
    status_code = 400
    error_code = "invalid_sync_payload"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class SyncSurfaceDatabaseError(SyncSurfaceError):
    """User-correctable database rejection from a sync surface payload."""


class Tier3SyncPayloadError(SyncSurfaceError):
    """Tier-3 data was presented to a Tier-2 cloud sync surface."""

    status_code = 422
    error_code = "tier3_sync_payload"


class PiiSyncPayloadError(SyncSurfaceError):
    """Raw personal data was presented to a non-PII cloud sync surface."""

    status_code = 422
    error_code = "pii_sync_payload"


class SyncConflictMetadataError(SyncSurfaceError):
    """A direct sync route received incomplete or invalid conflict metadata."""

    status_code = 422
    error_code = "missing_sync_conflict_metadata"


class SyncConflictError(SyncSurfaceError):
    """A workflow row has concurrent edits the server cannot merge safely."""

    status_code = 409
    error_code = "sync_conflict"


class SyncSurfaceNotFoundError(SyncSurfaceError):
    status_code = 404
    error_code = "not_found"


@dataclass(frozen=True)
class SurfaceDefinition:
    surface: str
    table: str
    pk_columns: tuple[str, ...]
    data_columns: tuple[str, ...]
    json_columns: tuple[str, ...]
    generated_pk: bool = False
    order_by: tuple[str, ...] = ("commit_seq",)


_ASYNC_PG_INPUT_ERRORS = (
    asyncpg.NotNullViolationError,
    asyncpg.CheckViolationError,
    asyncpg.ForeignKeyViolationError,
    asyncpg.UniqueViolationError,
    asyncpg.DataError,
    asyncpg.InvalidTextRepresentationError,
)
_PUBLIC_SYNC_ERROR_DETAIL_KEYS = frozenset({"surface", "column", "field", "id"})
_TIER3_VALUE_REDACTION = "[redacted-tier3-secret]"
_TIER3_PAYMENT_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "billing_zip",
        "card_label",
        "exp_month",
        "exp_year",
        "holder_name",
        "last4",
        "payment_methods",
    }
)
_TIER3_LOCAL_METADATA_KEYS: frozenset[str] = frozenset({"local_path"})
_TIER3_PAYMENT_METADATA_COMPACT_KEYS: frozenset[str] = frozenset(
    "".join(character for character in item if character.isalnum())
    for item in _TIER3_PAYMENT_METADATA_KEYS | _TIER3_LOCAL_METADATA_KEYS
)


def public_sync_error_details(exc: SyncSurfaceError) -> dict[str, Any] | None:
    """Return caller-safe error details without DB schema internals."""
    if not exc.details:
        return None
    details = {key: value for key, value in exc.details.items() if key in _PUBLIC_SYNC_ERROR_DETAIL_KEYS}
    return details or None


def _unique_violation_field(definition: SurfaceDefinition, exc: BaseException) -> str | None:
    constraint_name = str(getattr(exc, "constraint_name", "") or "")
    if definition.surface == "cloud_files" and "storage_key" in constraint_name:
        return "storage_key"
    if constraint_name.endswith("_user_id_id") or constraint_name.endswith("_pkey"):
        return definition.pk_columns[-1]
    return None


def _map_asyncpg_input_error(definition: SurfaceDefinition, exc: BaseException) -> SyncSurfaceDatabaseError:
    details = {"surface": definition.surface}
    column = getattr(exc, "column_name", None)
    if column:
        details["column"] = str(column)

    message = "Invalid %s payload." % definition.surface
    if isinstance(exc, asyncpg.NotNullViolationError):
        column = getattr(exc, "column_name", None)
        message = (
            "%s is required for %s." % (column, definition.surface)
            if column
            else "A required field is missing for %s." % definition.surface
        )
    elif isinstance(exc, asyncpg.CheckViolationError):
        message = "Invalid field value for %s." % definition.surface
    elif isinstance(exc, asyncpg.ForeignKeyViolationError):
        message = "%s references a missing row." % definition.surface
    elif isinstance(exc, asyncpg.UniqueViolationError):
        message = "%s conflicts with an existing row." % definition.surface
        field = _unique_violation_field(definition, exc)
        if field:
            details["field"] = field
    elif isinstance(exc, asyncpg.InvalidTextRepresentationError):
        message = "Invalid identifier or field type for %s." % definition.surface
    elif isinstance(exc, asyncpg.DataError):
        message = "Invalid field type for %s." % definition.surface

    return SyncSurfaceDatabaseError(message, details=details)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_size(value: Any) -> int:
    try:
        return len(_json_dumps(_json_ready(value)).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be JSON serializable") from exc


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    return value


def row_to_dict(row: Mapping[str, Any] | None, definition: SurfaceDefinition) -> JsonDict | None:
    if row is None:
        return None
    payload = {str(key): _json_ready(value) for key, value in dict(row).items()}
    for column in definition.json_columns:
        if column in payload:
            payload[column] = _json_ready(_json_loads(payload[column]))
    return payload


def rows_to_dicts(rows: Sequence[Mapping[str, Any]], definition: SurfaceDefinition) -> list[JsonDict]:
    return [row for item in rows if (row := row_to_dict(item, definition)) is not None]


def _has_disallowed_control(value: str) -> bool:
    return any(ord(character) < 32 and character not in "\t\r\n" for character in value)


def _reject_injection_string(value: str, *, path: str) -> None:
    if "\x00" in value or _has_disallowed_control(value):
        raise ValueError("%s contains a disallowed control character" % path)
    if _SCRIPT_TAG_RE.search(value):
        raise ValueError("%s contains disallowed script markup" % path)


def validate_sync_payload_shape(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    """Apply direct-route JSON bounds before a sync surface reaches PostgreSQL."""
    if depth == 0 and _json_size(value) > MAX_SYNC_DIRECT_ROW_JSON_BYTES:
        raise ValueError("%s is too large" % path)
    if depth > MAX_SYNC_DIRECT_JSON_DEPTH:
        raise ValueError("%s is too deeply nested" % path)
    if isinstance(value, Mapping):
        if len(value) > MAX_SYNC_DIRECT_JSON_OBJECT_KEYS:
            raise ValueError("%s has too many object keys" % path)
        for key, item in value.items():
            key_text = str(key)
            if not key_text:
                raise ValueError("%s contains an empty object key" % path)
            if len(key_text.encode("utf-8")) > MAX_SYNC_DIRECT_STRING_BYTES:
                raise ValueError("%s key is too large" % path)
            _reject_injection_string(key_text, path="%s key" % path)
            validate_sync_payload_shape(item, path="%s.%s" % (path, key_text), depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_SYNC_DIRECT_JSON_ARRAY_ITEMS:
            raise ValueError("%s has too many list items" % path)
        for index, item in enumerate(value):
            validate_sync_payload_shape(item, path="%s[%d]" % (path, index), depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_SYNC_DIRECT_STRING_BYTES:
            raise ValueError("%s string is too large" % path)
        _reject_injection_string(value, path=path)


def validate_sync_text_identifier(
    value: object,
    *,
    field: str,
    max_length: int = MAX_SYNC_DIRECT_IDENTIFIER_LENGTH,
) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("%s is required" % field)
    if len(text) > max_length:
        raise ValueError("%s is too long" % field)
    _reject_injection_string(text, path=field)
    return text


def validate_sync_device_id(value: object | None) -> str | None:
    if value is None:
        return None
    device_id = validate_sync_text_identifier(
        value,
        field="X-Device-Id",
        max_length=MAX_SYNC_DIRECT_DEVICE_ID_LENGTH,
    )
    if _DEVICE_ID_RE.fullmatch(device_id) is None:
        raise ValueError("X-Device-Id contains disallowed characters")
    return device_id


def _validate_client_hlc(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("lww_hlc must be a string")
    hlc = value.strip()
    match = _HLC_RE.fullmatch(hlc)
    if match is None:
        raise ValueError("lww_hlc must use '<ms>:<counter>:<actor_id>' format")
    ms = int(match.group("ms"))
    counter = int(match.group("counter"))
    actor_id = match.group("actor").strip()
    if not actor_id:
        raise ValueError("lww_hlc actor_id must be non-empty")
    if len(actor_id) > MAX_SYNC_DIRECT_HLC_ACTOR_ID_LENGTH:
        raise ValueError("lww_hlc actor_id is too long")
    if _has_disallowed_control(actor_id):
        raise ValueError("lww_hlc actor_id contains a disallowed control character")
    if counter > MAX_SYNC_DIRECT_HLC_COUNTER:
        raise ValueError("lww_hlc counter is too large")
    now_ms = int(time.time() * 1000)
    if ms > now_ms + MAX_SYNC_DIRECT_HLC_FUTURE_SKEW_MS:
        raise ValueError("lww_hlc is too far in the future")
    return hlc


def _validate_version_vector(value: Any) -> VersionVector:
    loaded = _json_loads(value) or {}
    if not isinstance(loaded, Mapping):
        raise ValueError("version_vector must be an object")
    if len(loaded) > MAX_SYNC_DIRECT_VERSION_VECTOR_ENTRIES:
        raise ValueError("version_vector has too many actors")
    normalized: VersionVector = {}
    for actor, counter in loaded.items():
        actor_id = validate_sync_text_identifier(
            actor,
            field="version_vector actor_id",
            max_length=MAX_SYNC_DIRECT_VERSION_VECTOR_ACTOR_LENGTH,
        )
        try:
            counter_value = int(counter)
        except (TypeError, ValueError) as exc:
            raise ValueError("version_vector counter must be an integer") from exc
        if counter_value < 0 or counter_value > MAX_SYNC_DIRECT_VERSION_VECTOR_COUNTER:
            raise ValueError("version_vector counter is out of range")
        normalized[actor_id] = counter_value
    return normalized


def _validate_field_versions(value: Any) -> dict[str, str]:
    loaded = _json_loads(value) or {}
    if not isinstance(loaded, Mapping):
        raise ValueError("field_versions must be an object")
    if len(loaded) > MAX_SYNC_DIRECT_FIELD_VERSION_ENTRIES:
        raise ValueError("field_versions has too many fields")
    normalized: dict[str, str] = {}
    for field_name, hlc in loaded.items():
        name = validate_sync_text_identifier(
            field_name,
            field="field_versions field name",
            max_length=MAX_SYNC_DIRECT_FIELD_VERSION_NAME_LENGTH,
        )
        normalized[name] = _validate_client_hlc(hlc)
    return normalized


def sanitize_token_metadata(payload: Any) -> Any:
    """Scrub legacy token metadata rows before returning them to clients."""
    if isinstance(payload, Mapping):
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            normalized = str(key).strip().lower()
            if normalized in TOKEN_METADATA_SENSITIVE_KEYS:
                continue
            if normalized.endswith("_token") and normalized != "token_type":
                continue
            if "access_token" in normalized or "refresh_token" in normalized:
                continue
            sanitized[str(key)] = sanitize_token_metadata(value)
        return sanitized
    if isinstance(payload, list):
        return [sanitize_token_metadata(item) for item in payload]
    if isinstance(payload, str) and is_tier3_secret_value(payload):
        return _TIER3_VALUE_REDACTION
    return payload


def is_tier3_sync_key(key: object) -> bool:
    normalized = unicodedata.normalize("NFKC", str(key)).strip().lower()
    compact = "".join(character for character in normalized if character.isalnum())
    if normalized in _TIER3_PAYMENT_METADATA_KEYS or normalized in _TIER3_LOCAL_METADATA_KEYS:
        return True
    if compact in _TIER3_PAYMENT_METADATA_COMPACT_KEYS:
        return True
    return is_tier3_credential_key(key)


def is_tier3_secret_value(value: object) -> bool:
    return is_tier3_credential_value(value)


def contains_payment_card_pan(value: object) -> bool:
    """Return True when text contains a Luhn-valid payment card number."""
    return boundary_contains_payment_card_pan(value)


def _sync_payload_path(parent: str, key: object) -> str:
    key_text = str(key)
    return key_text if not parent else "%s.%s" % (parent, key_text)


def is_pii_sync_key(key: object) -> bool:
    normalized = unicodedata.normalize("NFKC", str(key)).strip().lower()
    compact = "".join(character for character in normalized if character.isalnum())
    return normalized in PII_SYNC_EXACT_KEYS or compact in {
        "".join(character for character in candidate if character.isalnum()) for candidate in PII_SYNC_EXACT_KEYS
    }


def contains_low_ambiguity_pii(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return bool(_EMAIL_RE.search(value) or _SSN_RE.search(value))


def reject_pii_sync_payload(surface: str, payload: Any, *, path: str = "") -> None:
    """Reject obvious PII from sync surfaces that are documented as metadata-only."""
    if surface not in PII_DENYLIST_SYNC_SURFACES:
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_path = _sync_payload_path(path, key)
            if is_pii_sync_key(key):
                raise PiiSyncPayloadError(
                    "PII field is not allowed on %s: %s" % (surface, key_path),
                    details={"surface": surface, "field": key_path},
                )
            reject_pii_sync_payload(surface, value, path=key_path)
        return
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            reject_pii_sync_payload(surface, value, path="%s[%d]" % (path, index))
        return
    if contains_low_ambiguity_pii(payload):
        raise PiiSyncPayloadError(
            "PII value is not allowed on %s: %s" % (surface, path or "<value>"),
            details={"surface": surface, "field": path or "<value>"},
        )


def reject_tier3_sync_payload(surface: str, payload: Any, *, path: str = "") -> None:
    """Reject Tier-3-shaped fields on Tier-2 sync surfaces."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_path = _sync_payload_path(path, key)
            if is_tier3_sync_key(key):
                raise Tier3SyncPayloadError(
                    "Tier-3 field is not allowed on %s: %s" % (surface, key_path),
                    details={"surface": surface, "field": key_path},
                )
            if is_tier3_secret_value(value):
                raise Tier3SyncPayloadError(
                    "Tier-3 value is not allowed on %s: %s" % (surface, key_path),
                    details={"surface": surface, "field": key_path},
                )
            reject_tier3_sync_payload(surface, value, path=key_path)
        return
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            reject_tier3_sync_payload(surface, value, path="%s[%d]" % (path, index))
        return
    if contains_payment_card_pan(payload):
        field = path or "<value>"
        raise Tier3SyncPayloadError(
            "Tier-3 payment value is not allowed on %s: %s" % (surface, field),
            details={"surface": surface, "field": field},
        )
    if is_tier3_secret_value(payload):
        raise Tier3SyncPayloadError(
            "Tier-3 value is not allowed on %s: %s" % (surface, path or "<value>"),
            details={"surface": surface, "field": path or "<value>"},
        )


def redact_sync_payload(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if is_tier3_sync_key(key):
                redacted[str(key)] = "[redacted-tier3]"
            else:
                redacted[str(key)] = redact_sync_payload(value)
        return redacted
    if isinstance(payload, list):
        return [redact_sync_payload(value) for value in payload]
    if contains_payment_card_pan(payload):
        return "[redacted-tier3]"
    if isinstance(payload, str) and (
        is_tier3_secret_value(payload) or payload.strip().lower().startswith(("bearer ", "basic "))
    ):
        return "[redacted-credential]"
    return payload


def normalize_since(since_seq: int | str | None) -> int:
    if since_seq is None:
        return 0
    try:
        value = int(since_seq)
    except (TypeError, ValueError) as exc:
        raise ValueError("since must be an integer commit sequence") from exc
    return max(value, 0)


def normalize_limit(limit: int | str | None = None) -> int:
    if limit is None:
        return SYNC_LIMIT_DEFAULT
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return min(max(value, 1), SYNC_LIMIT_MAX)


def _payload_value(payload: Mapping[str, Any], column: str) -> Any:
    if column in payload:
        return payload[column]
    if column.endswith("_json"):
        stem = column[: -len("_json")]
        if stem in payload:
            return payload[stem]
    return None


def _payload_for_columns(payload: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in columns:
        value = _payload_value(payload, column)
        if value is not None:
            out[column] = value
    return out


def _field_columns(definition: SurfaceDefinition, payload: Mapping[str, Any]) -> tuple[str, ...]:
    present = tuple(column for column in definition.data_columns if _payload_value(payload, column) is not None)
    if present:
        return present
    return definition.data_columns


def _pk_where(definition: SurfaceDefinition, offset: int = 1) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for index, column in enumerate(definition.pk_columns, start=offset):
        clauses.append("%s = $%d" % (column, index))
        params.append(column)
    return " AND ".join(clauses), params


def _pk_values(definition: SurfaceDefinition, user_id: str, key: Any | None) -> list[Any]:
    values: list[Any] = []
    for column in definition.pk_columns:
        if column == "user_id":
            values.append(user_id)
        elif key is not None:
            values.append(key)
        else:
            raise ValueError("%s is required" % column)
    return values


def _row_pk(
    definition: SurfaceDefinition,
    row: Mapping[str, Any] | None,
    user_id: str,
    key: Any | None,
) -> str:
    if len(definition.pk_columns) == 1:
        return str(user_id)
    pk_column = definition.pk_columns[-1]
    if row is not None and pk_column in row:
        return str(row[pk_column])
    if key is not None:
        return str(key)
    return str(user_id)


def _snapshot_from_row(row: Mapping[str, Any] | None) -> ServerRowSnapshot | None:
    if row is None:
        return None
    return ServerRowSnapshot(
        version_vector=_json_loads(row.get("version_vector")) or {},
        lww_hlc=str(row.get("lww_hlc") or ""),
        lww_actor_id=str(row.get("lww_actor_id") or ""),
        last_mutation_id=str(row.get("last_mutation_id") or ""),
        field_versions=_json_loads(row.get("field_versions")) or {},
        deleted_at=row.get("deleted_at"),
    )


def incoming_from_payload(
    surface: str, op: Literal["upsert", "delete"], payload: Mapping[str, Any]
) -> IncomingMutation | None:
    present = _SYNC_MUTATION_REQUIRED_KEYS.intersection(payload)
    if present and not _SYNC_MUTATION_REQUIRED_KEYS.issubset(payload):
        missing = ", ".join(sorted(_SYNC_MUTATION_REQUIRED_KEYS.difference(payload)))
        raise SyncConflictMetadataError(
            "Sync conflict metadata is incomplete for %s; missing %s." % (surface, missing),
            details={"surface": surface},
        )
    if not present:
        return None
    actor_id = validate_sync_text_identifier(
        payload["lww_actor_id"],
        field="lww_actor_id",
        max_length=MAX_SYNC_DIRECT_HLC_ACTOR_ID_LENGTH,
    )
    last_mutation_id = validate_sync_text_identifier(
        payload["last_mutation_id"],
        field="last_mutation_id",
        max_length=MAX_SYNC_DIRECT_IDENTIFIER_LENGTH,
    )
    return IncomingMutation(
        surface=surface,
        op=op,
        row={str(key): value for key, value in payload.items()},
        version_vector=_validate_version_vector(payload.get("version_vector")),
        lww_hlc=_validate_client_hlc(payload["lww_hlc"]),
        lww_actor_id=actor_id,
        last_mutation_id=last_mutation_id,
        field_versions=_validate_field_versions(payload.get("field_versions")),
    )


def require_incoming_from_payload(
    surface: str,
    op: Literal["upsert", "delete"],
    payload: Mapping[str, Any],
) -> IncomingMutation:
    incoming = incoming_from_payload(surface, op, payload)
    if incoming is None:
        raise SyncConflictMetadataError(
            "Workflow writes require version_vector, lww_hlc, lww_actor_id, last_mutation_id, and field_versions.",
            details={"surface": surface},
        )
    return incoming


async def get_single_surface(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
) -> JsonDict | None:
    where_sql, _params = _pk_where(definition)
    row = await conn.fetchrow(
        "SELECT * FROM %s WHERE %s AND deleted_at IS NULL" % (definition.table, where_sql),  # nosec B608
        *_pk_values(definition, user_id, None),
    )
    return row_to_dict(row, definition)


async def list_keyed_surface(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
    since_seq: int | str | None = 0,
    *,
    limit: int | str | None = None,
) -> list[JsonDict]:
    since = normalize_since(since_seq)
    row_limit = normalize_limit(limit)
    order = ", ".join(definition.order_by)
    rows = await conn.fetch(
        "SELECT * FROM %s WHERE user_id = $1 AND commit_seq > $2 ORDER BY %s LIMIT $3"  # nosec B608
        % (definition.table, order),
        user_id,
        since,
        row_limit,
    )
    return rows_to_dicts(rows, definition)


async def get_keyed_surface(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
    key: Any,
) -> JsonDict | None:
    where_sql, _params = _pk_where(definition)
    row = await conn.fetchrow(
        "SELECT * FROM %s WHERE %s AND deleted_at IS NULL" % (definition.table, where_sql),  # nosec B608
        *_pk_values(definition, user_id, key),
    )
    return row_to_dict(row, definition)


async def _server_row(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
    key: Any | None,
) -> Mapping[str, Any] | None:
    where_sql, _params = _pk_where(definition)
    return await conn.fetchrow(
        "SELECT * FROM %s WHERE %s" % (definition.table, where_sql),  # nosec B608
        *_pk_values(definition, user_id, key),
    )


async def upsert_surface(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
    payload: Mapping[str, Any],
    incoming: IncomingMutation | None = None,
) -> JsonDict:
    validate_sync_payload_shape(payload, path=definition.surface)
    reject_tier3_sync_payload(definition.surface, payload)
    reject_pii_sync_payload(definition.surface, payload)
    if definition.surface == "metadata":
        metadata_key = validate_sync_text_identifier(payload.get("key", ""), field="key")
        if is_tier3_sync_key(metadata_key):
            raise Tier3SyncPayloadError(
                "Tier-3 metadata key is not allowed on metadata: %s" % metadata_key,
                details={"surface": definition.surface, "field": "key"},
            )
        if is_pii_sync_key(metadata_key):
            raise PiiSyncPayloadError(
                "PII metadata key is not allowed on metadata: %s" % metadata_key,
                details={"surface": definition.surface, "field": "key"},
            )
    key = payload.get(definition.pk_columns[-1]) if len(definition.pk_columns) > 1 else None
    server_row = None
    if key is not None or not definition.generated_pk:
        try:
            server_row = await _server_row(conn, definition, user_id, key)
        except _ASYNC_PG_INPUT_ERRORS as exc:
            raise _map_asyncpg_input_error(definition, exc) from exc
    if key is not None and definition.generated_pk and server_row is None:
        raise SyncSurfaceNotFoundError(
            "%s row %s was not found." % (definition.surface, key),
            details={"surface": definition.surface, "id": str(key)},
        )
    server_snapshot = _snapshot_from_row(server_row)

    if incoming is not None and server_snapshot is not None:
        if server_snapshot.last_mutation_id and server_snapshot.last_mutation_id == incoming.last_mutation_id:
            existing = row_to_dict(server_row, definition)
            if existing is not None:
                return existing
        outcome = resolve(server_snapshot, incoming)
        if outcome == ResolutionOutcome.IGNORE:
            existing = row_to_dict(server_row, definition)
            if existing is not None:
                return existing
        if outcome == ResolutionOutcome.MERGE and definition.surface in STRICT_CONFLICT_SYNC_SURFACES:
            raise SyncConflictError(
                "%s row has concurrent edits that must be retried with current state." % definition.surface,
                details={
                    "surface": definition.surface,
                    "id": _row_pk(definition, server_row, user_id, key),
                },
            )

    fields_touched = list(_field_columns(definition, payload))
    cloud_stamp = await stamp(
        conn,
        user_id,
        definition.surface,
        incoming,
        fields_touched,
        device_id_header=(str(payload.get("device_id")) if payload.get("device_id") else None),
        server_row=server_snapshot,
    )
    data = _payload_for_columns(payload, definition.data_columns)
    if not data:
        raise ValueError("payload must include at least one surface data field")

    try:
        row = await _write_upsert(conn, definition, user_id, key, data, cloud_stamp)
    except _ASYNC_PG_INPUT_ERRORS as exc:
        raise _map_asyncpg_input_error(definition, exc) from exc
    await write_journal(
        conn,
        user_id,
        definition.surface,
        _row_pk(definition, row, user_id, key),
        int(row["commit_seq"]),
        "upsert",
        str(row["last_mutation_id"]),
        changed_fields=fields_touched,
        version_vector=_json_loads(row["version_vector"]),
        field_versions=_json_loads(row["field_versions"]),
        updated_by_device_id=str(row["updated_by_device_id"]),
        client_hlc=str(row["lww_hlc"]),
    )
    result = row_to_dict(row, definition)
    if result is None:
        raise RuntimeError("upsert did not return a row")
    return result


async def _write_upsert(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
    key: Any | None,
    data: Mapping[str, Any],
    cloud_stamp: Any,
) -> Mapping[str, Any]:
    data_columns = tuple(column for column in definition.data_columns if column in data)
    insert_columns = ["user_id"]
    insert_values: list[Any] = [user_id]
    if len(definition.pk_columns) > 1 and key is not None:
        insert_columns.append(definition.pk_columns[-1])
        insert_values.append(key)
    for column in data_columns:
        insert_columns.append(column)
        value = data[column]
        insert_values.append(_json_dumps(value) if column in definition.json_columns else value)

    stamp_columns = [
        "consent_generation",
        "version_vector",
        "field_versions",
        "lww_hlc",
        "lww_actor_id",
        "updated_by_device_id",
        "last_mutation_id",
        "updated_at",
        "deleted_at",
        "commit_seq",
    ]
    insert_columns.extend(stamp_columns)
    insert_values.extend(
        [
            cloud_stamp.consent_generation,
            _json_dumps(cloud_stamp.version_vector),
            _json_dumps(cloud_stamp.field_versions),
            cloud_stamp.lww_hlc,
            cloud_stamp.lww_actor_id,
            cloud_stamp.updated_by_device_id,
            cloud_stamp.last_mutation_id,
            cloud_stamp.updated_at,
            None,
        ]
    )

    placeholders = [("$%d" % index) for index in range(1, len(insert_values) + 1)]
    placeholders.append("nextval('sync_commit_seq')")
    update_assignments = [
        "%s = EXCLUDED.%s" % (column, column) for column in (*data_columns, *stamp_columns) if column != "commit_seq"
    ]
    update_assignments.append("commit_seq = nextval('sync_commit_seq')")
    conflict_columns = ", ".join(definition.pk_columns)
    sql = (
        "INSERT INTO %(table)s (%(columns)s) VALUES (%(values)s) "  # nosec B608
        "ON CONFLICT (%(conflict)s) DO UPDATE SET %(updates)s RETURNING *"
        % {
            "table": definition.table,
            "columns": ", ".join(insert_columns),
            "values": ", ".join(placeholders),
            "conflict": conflict_columns,
            "updates": ", ".join(update_assignments),
        }
    )
    return await conn.fetchrow(sql, *insert_values)


async def delete_surface(
    conn: asyncpg.Connection,
    definition: SurfaceDefinition,
    user_id: str,
    key: Any | None = None,
    incoming: IncomingMutation | None = None,
) -> JsonDict | None:
    server_row = await _server_row(conn, definition, user_id, key)
    server_snapshot = _snapshot_from_row(server_row)
    if server_snapshot is None:
        return None
    if incoming is not None:
        if server_snapshot.last_mutation_id and server_snapshot.last_mutation_id == incoming.last_mutation_id:
            return row_to_dict(server_row, definition)
        outcome = resolve(server_snapshot, incoming)
        if outcome == ResolutionOutcome.IGNORE:
            return row_to_dict(server_row, definition)
    cloud_stamp = await stamp(
        conn,
        user_id,
        definition.surface,
        incoming,
        ["deleted_at"],
        server_row=server_snapshot,
    )
    where_sql, _params = _pk_where(definition)
    row = await conn.fetchrow(
        (
            "UPDATE %(table)s SET deleted_at = $%(deleted_param)d, updated_at = $%(updated_param)d, "  # nosec B608
            "consent_generation = $%(consent_param)d, version_vector = $%(vv_param)d, "
            "field_versions = $%(fv_param)d, lww_hlc = $%(hlc_param)d, lww_actor_id = $%(actor_param)d, "
            "updated_by_device_id = $%(device_param)d, last_mutation_id = $%(mutation_param)d, "
            "commit_seq = nextval('sync_commit_seq') WHERE %(where)s RETURNING *"
            % {
                "table": definition.table,
                "deleted_param": len(definition.pk_columns) + 1,
                "updated_param": len(definition.pk_columns) + 2,
                "consent_param": len(definition.pk_columns) + 3,
                "vv_param": len(definition.pk_columns) + 4,
                "fv_param": len(definition.pk_columns) + 5,
                "hlc_param": len(definition.pk_columns) + 6,
                "actor_param": len(definition.pk_columns) + 7,
                "device_param": len(definition.pk_columns) + 8,
                "mutation_param": len(definition.pk_columns) + 9,
                "where": where_sql,
            }
        ),
        *_pk_values(definition, user_id, key),
        cloud_stamp.updated_at,
        cloud_stamp.updated_at,
        cloud_stamp.consent_generation,
        _json_dumps(cloud_stamp.version_vector),
        _json_dumps(cloud_stamp.field_versions),
        cloud_stamp.lww_hlc,
        cloud_stamp.lww_actor_id,
        cloud_stamp.updated_by_device_id,
        cloud_stamp.last_mutation_id,
    )
    if row is not None:
        await write_journal(
            conn,
            user_id,
            definition.surface,
            _row_pk(definition, row, user_id, key),
            int(row["commit_seq"]),
            "delete",
            str(row["last_mutation_id"]),
            changed_fields=["deleted_at"],
            version_vector=_json_loads(row["version_vector"]),
            field_versions=_json_loads(row["field_versions"]),
            updated_by_device_id=str(row["updated_by_device_id"]),
            client_hlc=str(row["lww_hlc"]),
        )
    return row_to_dict(row, definition)
