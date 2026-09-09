"""Helpers for normalizing and retrying native tool-call argument payloads."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class EmptyToolArgumentsRetry:
    """Retry prompt metadata for a tool call with missing required args."""

    tool_name: str
    required_args: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class ToolCallArguments:
    """One native tool call's arguments, normalized, plus what was wrong.

    ``was_empty`` and ``was_malformed`` are mutually exclusive: a payload
    either carried no arguments at all (empty) or carried something that is
    not a usable arguments object (malformed). Both feed
    :func:`build_empty_tool_arguments_retry`.
    """

    args: dict[str, Any]
    was_empty: bool = False
    was_malformed: bool = False


_EMPTY_ARGUMENTS = ToolCallArguments(args={}, was_empty=True)
_MALFORMED_ARGUMENTS = ToolCallArguments(args={}, was_malformed=True)


def _from_decoded_arguments(decoded: Any) -> ToolCallArguments:
    """Classify an already-decoded arguments value."""

    if not isinstance(decoded, Mapping):
        # Valid JSON that is not an object (a list, number, string, bool, or
        # null) is not an arguments payload. Passing it through as ``args``
        # hands the tool executor a non-dict it cannot bind to parameters.
        return _MALFORMED_ARGUMENTS
    if not all(isinstance(key, str) for key in decoded):
        # A JSON object always has string keys; anything else came from a
        # provider shape we cannot bind to named parameters.
        return _MALFORMED_ARGUMENTS
    args = dict(decoded)
    # Some models emit a ``reasoning`` pseudo-argument alongside (or instead
    # of) the real ones. It is never a tool parameter, so it is dropped
    # before emptiness is judged: a call carrying only ``reasoning`` supplied
    # no arguments at all.
    args.pop("reasoning", None)
    if not args:
        return _EMPTY_ARGUMENTS
    return ToolCallArguments(args=args)


def coerce_tool_call_arguments(raw_arguments: Any) -> ToolCallArguments:
    """Normalize any provider's tool-call ``arguments`` payload to a dict.

    OpenAI's own endpoints send ``arguments`` as a JSON *string*, but the
    OpenAI-compatible third-party servers BYOK users point at are looser: some
    send the already-parsed object, some send ``null``, some send a scalar.
    The SDK's lenient model construction passes those through untouched, so
    every shape has to be handled here rather than assumed away.

    - ``None`` / blank string / ``{}`` / an object holding only ``reasoning``
      -> empty. The model supplied no arguments; that is legitimate for a tool
      with no required params and worth a retry otherwise.
    - JSON-object string, or an already-parsed mapping -> those arguments are
      used as-is. An already-parsed mapping is real model intent; collapsing
      it to ``{}`` silently executes the tool with nothing.
    - unparseable string, non-object JSON, or any other type (int, list,
      bool) -> malformed. The model meant to send arguments and the payload
      is unusable, so the caller re-prompts instead of dispatching ``{}``.
    """

    if raw_arguments is None:
        return _EMPTY_ARGUMENTS
    if isinstance(raw_arguments, str):
        text = raw_arguments.strip()
        if not text:
            return _EMPTY_ARGUMENTS
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            return _MALFORMED_ARGUMENTS
        return _from_decoded_arguments(decoded)
    if isinstance(raw_arguments, Mapping):
        return _from_decoded_arguments(raw_arguments)
    return _MALFORMED_ARGUMENTS


def _tool_name(schema: Mapping[str, Any]) -> str:
    if isinstance(schema.get("function"), Mapping):
        return str(schema["function"].get("name") or "")
    return str(schema.get("name") or "")


def _tool_parameters(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(schema.get("inputSchema"), Mapping):
        return schema["inputSchema"]
    if isinstance(schema.get("parameters"), Mapping):
        return schema["parameters"]
    function = schema.get("function")
    if isinstance(function, Mapping) and isinstance(function.get("parameters"), Mapping):
        return function["parameters"]
    return {}


def _required_arguments(schema: Mapping[str, Any]) -> tuple[str, ...]:
    parameters = _tool_parameters(schema)
    required = parameters.get("required")
    if isinstance(required, list):
        return tuple(str(item) for item in required if str(item).strip())
    if isinstance(required, tuple):
        return tuple(str(item) for item in required if str(item).strip())
    return ()


def _parameter_description(schema: Mapping[str, Any], param_name: str) -> str:
    parameters = _tool_parameters(schema)
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    prop = properties.get(param_name)
    if not isinstance(prop, Mapping):
        return ""
    return str(prop.get("description") or "").strip()


def _find_tool_schema(tools: list[dict[str, Any]], tool_name: str) -> Mapping[str, Any] | None:
    for schema in tools:
        if isinstance(schema, Mapping) and _tool_name(schema) == tool_name:
            return schema
    return None


def _tool_calls_for_retry(parsed: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    calls = parsed.get("_all_tool_calls")
    if isinstance(calls, list):
        return [call for call in calls if isinstance(call, Mapping)]
    if parsed.get("type") == "tool_call":
        return [parsed]
    return []


def build_empty_tool_arguments_retry(
    parsed: Mapping[str, Any],
    tools: list[dict[str, Any]],
) -> EmptyToolArgumentsRetry | None:
    """Build one schema-aware retry prompt for the first bad-argument tool call.

    Fires for two argument defects the parser flags on a native tool call:

    - ``_arguments_were_empty`` — the model emitted a blank/absent argument
      payload. Only worth a retry when the tool has required params (a no-arg
      call to a no-required-param tool is legitimate).
    - ``_arguments_were_malformed`` — the model emitted a NON-empty arguments
      string that failed to parse as JSON (truncated/malformed). Always worth a
      retry: the model clearly intended arguments but produced invalid JSON, and
      silently collapsing that to ``{}`` loses its intent regardless of whether
      the schema marks any param required.

    The retry message states the mechanical defect (empty vs. invalid JSON) and
    asks the model to re-emit the call. It is provider error feedback about the
    model's own machine output shape, not a query/intent classifier, output
    keyword parser, or steering hint.
    """

    for call in _tool_calls_for_retry(parsed):
        was_empty = bool(call.get("_arguments_were_empty"))
        was_malformed = bool(call.get("_arguments_were_malformed"))
        if not (was_empty or was_malformed):
            continue
        tool_name = str(call.get("tool") or "")
        if not tool_name:
            continue
        schema = _find_tool_schema(tools, tool_name)
        if schema is None:
            continue
        required = _required_arguments(schema)
        # A blank call to a tool with no required params is a legitimate no-arg
        # call — never retry it. Malformed JSON is always broken, so retry it
        # even when no param is marked required.
        if was_empty and not required:
            continue
        details = []
        for arg_name in required:
            desc = _parameter_description(schema, arg_name)
            if desc:
                details.append("%s: %s" % (arg_name, desc[:180]))
            else:
                details.append(arg_name)
        if was_malformed:
            if details:
                message = (
                    "You called %s with arguments that were not valid JSON (they were "
                    "truncated or malformed). Retry the tool call now with a single "
                    "well-formed JSON object that includes the required parameter(s): %s. "
                    "Extract the values from the user's request." % (tool_name, "; ".join(details))
                )
            else:
                message = (
                    "You called %s with arguments that were not valid JSON (they were "
                    "truncated or malformed). Retry the tool call now with a single "
                    "well-formed JSON object of the arguments." % tool_name
                )
        else:
            message = (
                "You called %s with no arguments. Retry the tool call now with a JSON object "
                "that includes the required parameter(s): %s. Extract the values from the "
                "user's request and do not call %s with {} or an empty argument string."
                % (tool_name, "; ".join(details), tool_name)
            )
        return EmptyToolArgumentsRetry(tool_name=tool_name, required_args=required, message=message)
    return None


def append_responses_retry_input(api_kwargs: dict[str, Any], message: str) -> dict[str, Any]:
    """Copy a Responses API payload and append a user retry instruction."""

    retry_kwargs = copy.deepcopy(api_kwargs)
    retry_input = list(retry_kwargs.get("input") or [])
    retry_input.append({"role": "user", "content": [{"type": "input_text", "text": message}]})
    retry_kwargs["input"] = retry_input
    return retry_kwargs


def append_chat_retry_message(messages: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
    """Copy a Chat Completions history and append a user retry instruction.

    The Chat-Completions twin of :func:`append_responses_retry_input`, so the
    same retry prompt reaches the model on either API surface.
    """

    retry_messages = [dict(entry) for entry in messages]
    retry_messages.append({"role": "user", "content": message})
    return retry_messages
