from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping, Sequence
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import JsonDict, JsonValue, to_json_value
from models.player import PlayerState, validate_player_state_payload

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "core" / "schemas" / "player_state.json"
FALLBACK_ERROR_CODE = "player_state_schema_violation"


class PlayerStateContractError(RuntimeError):
    """Raised when the player state payload violates the published contract."""

    def __init__(self, errors: Sequence[str]):
        details = list(errors)
        message = "; ".join(details) if details else "player state contract violated"
        super().__init__(message)
        self.errors = details


@lru_cache(maxsize=1)
def _load_schema() -> JsonDict:
    try:
        schema_value = to_json_value(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        if isinstance(schema_value, dict):
            return schema_value
        raise RuntimeError(f"Player state schema at {SCHEMA_PATH} is not a JSON object.")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Player state schema missing at {SCHEMA_PATH}. Run `python scripts/generate_schema.py`."
        ) from exc


@lru_cache(maxsize=1)
def _schema_validator() -> Draft202012Validator:
    return Draft202012Validator(_load_schema())


class PlayerStateValidator:
    """Shared contract enforcement utilities for player state payloads."""

    def __init__(self) -> None:
        self._validator: Draft202012Validator | None = None

    def _ensure_validator(self) -> Draft202012Validator:
        """Lazily compile the JSON schema validator on first use."""
        if self._validator is None:
            self._validator = _schema_validator()
        return self._validator

    def validate(self, payload: Mapping[str, JsonValue]) -> PlayerState:
        """
        Validate the payload against the JSON Schema and Pydantic model.

        Raises:
            PlayerStateContractError: when the payload cannot be coerced into a
            valid PlayerState.
        """

        errors = []
        stripped_payload: JsonDict = {
            key: to_json_value(value)
            for key, value in payload.items()
            if key not in {"ok", "error", "data", "fallback"}
        }

        for error in self._ensure_validator().iter_errors(stripped_payload):
            location = "/".join(str(x) for x in error.path) or "<root>"
            errors.append(f"{location}: {error.message}")

        if errors:
            raise PlayerStateContractError(errors)

        try:
            return validate_player_state_payload(payload)
        except PydanticValidationError as exc:  # pragma: no cover - explicit error path
            raise PlayerStateContractError([str(exc)]) from exc

    def sanitize(
        self,
        payload: Mapping[str, JsonValue],
        *,
        on_error: bool = False,
    ) -> JsonDict:
        """
        Return a sanitized mapping suitable for serialization.

        When `on_error` is true the payload is assumed to represent a fallback
        state provided by the contract middleware.
        """

        base = dict(payload)
        if not on_error:
            base.setdefault("ok", True)
            base.setdefault("error", None)
        return base


def ensure_player_state_schema() -> None:
    """
    Ensure the schema artefact is available.

    Intended for startup hooks so we fail fast when the schema is missing.
    """

    _load_schema()


# NOTE: get_player_state_schema() is now defined in models/player.py
# Import from there: from models.player import get_player_state_schema


def serialize_player_state(state: PlayerState) -> JsonDict:
    """Return a serialisable representation of the player state."""

    dumped_value = to_json_value(state.model_dump())
    return dumped_value if isinstance(dumped_value, dict) else {}


def sanitize_player_state_payload(
    payload: Mapping[str, JsonValue],
    *,
    validator: PlayerStateValidator | None = None,
) -> ResponseEnvelope:
    active_validator = validator or PlayerStateValidator()
    try:
        state = active_validator.validate(payload)
        state_payload = serialize_player_state(state)
        data: JsonDict = {
            **state_payload,
            "schema": {"path": str(SCHEMA_PATH.name)},
        }
        return success_response(to_json_value(data))
    except PlayerStateContractError as exc:
        fallback_state = PlayerState()
        fallback_payload = serialize_player_state(fallback_state)
        fallback_data: MutableMapping[str, JsonValue] = {
            **fallback_payload,
            "schema": {"path": str(SCHEMA_PATH.name)},
            "fallback": fallback_payload,
        }
        return failure_response(
            FALLBACK_ERROR_CODE,
            "Player state payload violated the published schema.",
            details={"errors": list(exc.errors)},
            data=to_json_value(fallback_data),
        )
