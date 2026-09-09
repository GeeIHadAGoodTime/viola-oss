"""
JSON type aliases and safe conversion helpers.

This module provides a canonical, untyped-safe way to represent JSON-like data across the repo.
Uses Pydantic's JsonValue for compatibility with Pydantic models.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from pydantic.types import JsonValue as _PydanticJsonValue

# Core type aliases - use Pydantic's JsonValue for model compatibility
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = _PydanticJsonValue
JSONObject: TypeAlias = dict[str, JSONValue]

# Backward-compatible aliases (prefer uppercase variants for new code)
JsonScalar: TypeAlias = JSONScalar
JsonValue: TypeAlias = JSONValue
JsonObject: TypeAlias = JSONObject
JsonDict: TypeAlias = dict[str, JSONValue]


def to_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, Enum):
        enum_value = value.value
        if enum_value is None or isinstance(enum_value, (str, int, float, bool)):
            return enum_value
        return str(enum_value)

    if is_dataclass(value) and not isinstance(value, type):
        # is_dataclass returns True for both instances and classes, so check it's not a type
        return to_json_value(asdict(value))

    if isinstance(value, dict):
        out: JSONObject = {}
        for key, item in value.items():
            out[str(key)] = to_json_value(item)
        return out

    if isinstance(value, (list, tuple)):
        return [to_json_value(item) for item in value]

    return str(value)


def to_json_object(mapping: Mapping[str, object]) -> JSONObject:
    out: JSONObject = {}
    for key, value in mapping.items():
        out[key] = to_json_value(value)
    return out
