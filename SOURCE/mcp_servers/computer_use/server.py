"""Desktop computer-use MCP server."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from core.logging_config import get_logger
from intent.tools.desktop import desktop_hotkey as _desktop_hotkey_tool, desktop_type_text as _desktop_type_text_tool
from services.computer_use import app_launcher, input as computer_input, safety, screen, window_manager
from services.computer_use.cloud_guard import assert_desktop_surface, assert_local_user

logger = get_logger(__name__)

server = FastMCP("viola-computer-use")

_DANGEROUS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
_CONFIRM = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_CHAIN_COUNTS: dict[str, int] = {}
_HELD_LOW_LEVEL_KEYS: dict[str, list[str]] = {}
_HELD_LOW_LEVEL_MOUSE_BUTTONS: dict[str, list[str]] = {}
_LOW_LEVEL_MOUSE_BUTTONS = frozenset({"left", "right", "middle"})

ComputerAction = Literal[
    "launch_app",
    "list_windows",
    "screenshot",
    "observe_region",
    "analyze_screen",
    "focus_window",
    "read_window",
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "scroll",
    "volume",
    "mouse_move",
    "drag",
    "wait",
    # New ref-based architecture (2026-05-01):
    "inspect_window",  # combined snapshot + screenshot + optional vision
    "click_ref",  # deterministic click by snapshot ref
    "background_type",  # send_chars without focus steal
    "background_screenshot",  # PrintWindow API, captures minimized windows
    # Low-level input primitives (2026-05-02): general computer/game control.
    # Anthropic ships hold_key + left_mouse_down/up; Cradle ships matching
    # mouse_hold/release + key_press/release. Convergence of two independent
    # frameworks. Plus mouse_move_relative which Anthropic LACKS — required
    # for cursor-locked games (Minecraft, FPS, sandbox, CAD, drone sim).
    "mouse_move_relative",
    "mouse_button_down",
    "mouse_button_up",
    "mouse_button_hold",
    "key_down",
    "key_up",
    "key_hold",
    "mouse_move_angle",
]

_ACTIONS_WITHOUT_FOREGROUND_TARGET: frozenset[str] = frozenset(
    {"launch_app", "volume", "analyze_screen", "list_windows"}
)


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _get_call_meta() -> dict[str, object]:
    """Return request metadata for the current MCP tool invocation."""
    try:
        context = server.get_context()
        request_context = context.request_context
        meta = getattr(request_context, "meta", None)
        if meta is None:
            return {}
        if hasattr(meta, "model_dump"):
            dumped = meta.model_dump()
            return dumped if isinstance(dumped, dict) else {}
        if isinstance(meta, dict):
            return meta
    except Exception:
        logger.debug("No MCP request metadata available for computer-use call")
    return {}


def _get_call_user_id() -> str:
    """Extract user_id from MCP metadata, falling back to the desktop device user."""
    from core.user_context import get_current_or_device_user_id, user_id_or_none

    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        user_id = user_id_or_none(user_context.get("user_id"))
        if user_id is not None:
            return user_id
    user_id = user_id_or_none(meta.get("user_id"))
    if user_id is not None:
        return user_id
    return get_current_or_device_user_id()


def _get_chain_key(user_id: str) -> str:
    return "%s:%s" % (user_id, _get_call_session_id(user_id))


def _get_call_session_id(user_id: str) -> str:
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        for key in ("session_id", "request_id", "gate_session_id"):
            value = user_context.get(key)
            if isinstance(value, str) and value:
                return value
    from core.user_context import is_device_user_id

    return "local-session" if is_device_user_id(user_id) else user_id


def _next_chain_status(user_id: str) -> dict[str, object]:
    key = _get_chain_key(user_id)
    count = _CHAIN_COUNTS.get(key, 0) + 1
    _CHAIN_COUNTS[key] = count
    return safety.action_chain_status(count, user_id=user_id)


def _low_level_chain_key(user_id: str) -> str:
    return _get_chain_key(user_id)


def _held_low_level_mouse_buttons(user_id: str) -> list[str]:
    return _HELD_LOW_LEVEL_MOUSE_BUTTONS.setdefault(_low_level_chain_key(user_id), [])


def _normalize_low_level_mouse_button(button: str, *, action: str, target_app_executable: str) -> str:
    normalized = (button or "left").strip().lower()
    if normalized not in _LOW_LEVEL_MOUSE_BUTTONS:
        raise safety.ComputerUseRefusal(
            error_category="COMPUTER_USE_INVALID_ARGUMENTS",
            reason="mouse button must be one of: left, middle, right",
            action=action,
            target_app_executable=target_app_executable,
        )
    return normalized


def _low_level_key_tokens(key: str) -> list[str]:
    return [part for part in safety.normalize_key_chord(key).split("+") if part]


def _held_low_level_keys(user_id: str) -> list[str]:
    return _HELD_LOW_LEVEL_KEYS.setdefault(_low_level_chain_key(user_id), [])


def _track_low_level_key_down(user_id: str, key: str) -> None:
    held = _held_low_level_keys(user_id)
    for token in _low_level_key_tokens(key):
        if token not in held:
            held.append(token)


def _track_low_level_key_up(user_id: str, key: str) -> None:
    tokens = set(_low_level_key_tokens(key))
    held = _HELD_LOW_LEVEL_KEYS.get(_low_level_chain_key(user_id), [])
    _HELD_LOW_LEVEL_KEYS[_low_level_chain_key(user_id)] = [item for item in held if item not in tokens]


def _track_low_level_mouse_down(user_id: str, button: str) -> None:
    held = _held_low_level_mouse_buttons(user_id)
    if button not in held:
        held.append(button)


def _track_low_level_mouse_up(user_id: str, button: str) -> None:
    held = _HELD_LOW_LEVEL_MOUSE_BUTTONS.get(_low_level_chain_key(user_id), [])
    _HELD_LOW_LEVEL_MOUSE_BUTTONS[_low_level_chain_key(user_id)] = [item for item in held if item != button]


def _release_held_low_level_keys(user_id: str) -> None:
    held = _HELD_LOW_LEVEL_KEYS.pop(_low_level_chain_key(user_id), [])
    safety.clear_low_level_held_keys(user_id=user_id, session_id=_get_call_session_id(user_id))
    if not held:
        return
    try:
        from intent.tools import desktop_input_low
    except ImportError:
        logger.debug("Could not import low-level input module while releasing held keys")
        return
    for held_key in reversed(held):
        try:
            desktop_input_low.key_up(held_key)
        except (OSError, RuntimeError, ValueError):
            logger.debug("Failed to release held low-level key %s", held_key)


def _release_held_low_level_mouse_buttons(user_id: str) -> None:
    held = _HELD_LOW_LEVEL_MOUSE_BUTTONS.pop(_low_level_chain_key(user_id), [])
    if not held:
        return
    try:
        from intent.tools import desktop_input_low
    except ImportError:
        logger.debug("Could not import low-level input module while releasing held mouse buttons")
        return
    for held_button in reversed(held):
        try:
            desktop_input_low.mouse_button_up(held_button)
        except (OSError, RuntimeError, ValueError):
            logger.debug("Failed to release held low-level mouse button %s", held_button)


def _release_held_low_level_input(user_id: str) -> None:
    _release_held_low_level_mouse_buttons(user_id)
    _release_held_low_level_keys(user_id)


def reset_low_level_input_state_for_tests() -> None:
    _HELD_LOW_LEVEL_KEYS.clear()
    _HELD_LOW_LEVEL_MOUSE_BUTTONS.clear()


def reset_low_level_key_state_for_tests() -> None:
    reset_low_level_input_state_for_tests()


def _envelope_from_exception(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, safety.ComputerUseRefusal):
        return exc.to_envelope()
    if isinstance(exc, NotImplementedError) and exc.args and isinstance(exc.args[0], dict):
        return dict(exc.args[0])
    if isinstance(exc, PermissionError) and exc.args and isinstance(exc.args[0], dict):
        return dict(exc.args[0])
    return {
        "ok": False,
        "error_category": "COMPUTER_USE_ERROR",
        "reason": str(exc) or exc.__class__.__name__,
    }


def _coordinate_required(action: str, coordinate: list[int] | None) -> list[int]:
    if coordinate is None:
        raise safety.ComputerUseRefusal(
            error_category="COMPUTER_USE_INVALID_ARGUMENTS",
            reason="coordinate is required for %s" % action,
            action=action,
        )
    return coordinate


def _coordinate_from_xy(
    action: str,
    coordinate: list[int] | None,
    x: int | None,
    y: int | None,
) -> list[int]:
    if coordinate is not None:
        return coordinate
    if x is not None and y is not None:
        return [int(x), int(y)]
    return _coordinate_required(action, coordinate)


def _coordinate_from_optional_xy(
    coordinate: list[int] | None,
    x: int | None,
    y: int | None,
) -> list[int] | None:
    if coordinate is not None:
        return coordinate
    if x is not None and y is not None:
        return [int(x), int(y)]
    return None


def _required_key_argument(action: str, key_text: str) -> str:
    normalized = str(key_text or "").strip()
    if not normalized:
        raise safety.ComputerUseRefusal(
            error_category="COMPUTER_USE_INVALID_ARGUMENTS",
            reason="key is required for %s" % action,
            action=action,
        )
    return normalized


def _target_window_title(
    *,
    target_window: str,
    window_title: str,
    title: str,
) -> str:
    return (target_window or window_title or title or "").strip()


def _tool_result_payload(result: object) -> dict[str, object]:
    ok = bool(getattr(result, "ok", False))
    data = getattr(result, "data", None)
    error = getattr(result, "error", None)
    # ``unverified`` is the third answer: the tool ran and could not
    # establish the outcome. It has to survive the flattening from
    # ToolResult into this JSON envelope, or the distinction dies here and
    # the model reads a plain success.
    unverified = bool(getattr(result, "unverified", False))
    if ok:
        if isinstance(data, dict):
            payload = dict(data)
            payload.setdefault("ok", True)
            if unverified:
                payload["unverified"] = True
            return payload
        payload = {"ok": True, "result": data}
        if unverified:
            payload["unverified"] = True
        return payload
    failure: dict[str, object] = {"ok": False, "error": str(error or "Tool action failed")}
    if isinstance(data, dict):
        # Carry the evidence of the failure (accepted-vs-requested event
        # counts, the target window we aimed at) rather than only its prose.
        for key in ("events_requested", "events_accepted", "method", "target_hwnd", "target_source"):
            if key in data:
                failure[key] = data[key]
    if unverified:
        failure["unverified"] = True
    return failure


async def _desktop_read_window(title: str) -> dict[str, object]:
    from intent.tools.desktop import desktop_read_window

    return _tool_result_payload(await desktop_read_window(title))


async def _desktop_click_name(name: str, window_title: str) -> dict[str, object]:
    from intent.tools.desktop import desktop_click_element

    return _tool_result_payload(await desktop_click_element(name, window_title))


async def _desktop_type(text: str, *, respect_focus: bool, window_title: str) -> dict[str, object]:
    return _tool_result_payload(
        await _desktop_type_text_tool(text, respect_focus=respect_focus, window_title=window_title)
    )


async def _desktop_hotkey(keys: str) -> dict[str, object]:
    return _tool_result_payload(await _desktop_hotkey_tool(keys))


async def _desktop_mouse_click(x: int, y: int, button: str) -> dict[str, object]:
    from intent.tools.desktop import desktop_mouse_click

    return _tool_result_payload(await desktop_mouse_click(x, y, button))


async def _desktop_screenshot(region: str) -> dict[str, object]:
    from intent.tools.desktop import desktop_screenshot

    return _tool_result_payload(await desktop_screenshot(region))


async def _desktop_volume(
    *,
    action: str,
    percent: int | None,
    mute: bool | None,
    step: int,
) -> dict[str, object]:
    from intent.tools.system_volume import desktop_volume

    return _tool_result_payload(await desktop_volume(action=action, percent=percent, mute=mute, step=step))


async def _analyze_screen(question: str, capture_mode: str) -> dict[str, object]:
    from intent.tools.vision_tools import analyze_screen

    result = await analyze_screen(question=question, capture_mode=capture_mode)
    if isinstance(result, dict):
        payload = dict(result)
        payload.setdefault("ok", bool(payload.get("success", True)))
        return payload
    return {"ok": True, "result": result}


def _scroll_deltas(direction: str, amount: int, scroll_x: int, scroll_y: int) -> tuple[int, int]:
    if scroll_x or scroll_y:
        return int(scroll_x), int(scroll_y)
    normalized = direction.strip().lower()
    bounded_amount = int(amount or 0)
    if normalized == "up":
        return 0, abs(bounded_amount)
    if normalized == "down":
        return 0, -abs(bounded_amount)
    if normalized == "left":
        return -abs(bounded_amount), 0
    if normalized == "right":
        return abs(bounded_amount), 0
    return 0, bounded_amount


def _dispatch_envelope(dispatch: object, **echoed: object) -> dict[str, object]:
    """Build a low-level input envelope from what Windows did, not from the request.

    Every one of these branches used to read ``{"ok": True, <the arguments
    the caller passed in>}``: the button they asked for, the key they asked
    for, the duration they asked for. None of that was evidence. Windows
    had already told ``SendInput`` how many events it accepted -- zero is
    the routine answer when the foreground window runs at higher integrity
    than Viola (UIPI) or input is blocked -- and that answer was thrown
    away one layer down, so a keystroke that never existed came back as a
    success the model then reported to the user.

    Now ``ok`` comes from the accepted count, a refusal carries the Win32
    reason, and an accepted event that nothing could observe is marked
    ``unverified`` rather than rounded up. The echoed arguments stay, since
    the model needs to know WHICH key we tried, but they no longer decide
    the verdict.
    """
    envelope: dict[str, object] = dict(echoed)
    requested = getattr(dispatch, "requested", None)
    accepted = getattr(dispatch, "accepted", None)
    if requested is None or accepted is None:
        # No dispatch information at all. That is not a success; it is an
        # absence of evidence, and it is reported as one.
        envelope["ok"] = True
        envelope["unverified"] = True
        envelope["unverified_reason"] = "the input backend returned no delivery information"
        return envelope

    envelope["events_requested"] = int(requested)
    envelope["events_accepted"] = int(accepted)
    if not dispatch.submitted:
        envelope["ok"] = False
        envelope["error_category"] = "COMPUTER_USE_INPUT_REFUSED"
        envelope["error"] = dispatch.refusal_reason() or "Windows refused the synthesized input"
        envelope["reason"] = envelope["error"]
        return envelope

    envelope["ok"] = True
    blind = getattr(dispatch, "blind_reason", None)
    if blind:
        envelope["unverified"] = True
        envelope["unverified_reason"] = blind
    else:
        # Accepted at the boundary. That is everything an injection-side
        # call can know: SendInput has no channel back from the target's
        # message pump, so "the app reacted" is not on offer here.
        envelope["unverified"] = True
        envelope["unverified_reason"] = (
            "Windows accepted the event, which is as far as input synthesis can see; nothing observed "
            "the target application receive or act on it"
        )
    return envelope


def _focus_precondition(result: object, *, action: str) -> dict[str, object] | None:
    """Return a refusal envelope when a pre-action focus attempt did not take.

    ``click``, ``key`` and ``scroll`` call ``focus_window`` first and then
    discarded its envelope entirely, so a focus Windows refused was followed
    by clicking and typing into whatever window happened to be in front --
    the single most common way a keystroke lands in the wrong place. If we
    could not put the requested window in front, doing the action anyway is
    how the user's text ends up in a chat window instead of the editor.
    """
    if not isinstance(result, dict):
        return None
    if result.get("ok") is not False:
        return None
    reason = str(result.get("reason") or result.get("error") or "the window could not be focused")
    return {
        "ok": False,
        "action": action,
        "error_category": str(result.get("error_category") or "COMPUTER_USE_WINDOW_NOT_FOUND"),
        "reason": "Refused to %s: %s" % (action, reason),
        "foreground_window_title": result.get("foreground_window_title", ""),
    }


def _merge_action_metadata(
    result: dict[str, object],
    *,
    action: str,
    target_app_executable: str,
    duration_ms: int,
    chain_status: dict[str, object],
    control_indicator: dict[str, object] | None = None,
) -> dict[str, object]:
    result.setdefault("ok", True)
    result["action"] = action
    result["target_app_executable"] = target_app_executable
    result["duration_ms"] = duration_ms
    if chain_status.get("warning"):
        result["warning"] = str(chain_status["warning"])
    if control_indicator is not None:
        result["control_indicator"] = control_indicator
    return result


@server.tool(
    name="computer",
    annotations=_DANGEROUS,
    description=(
        "Unified desktop GUI control for the local Windows computer. "
        "Observation — variants: list_windows / screenshot(region) / "
        "observe_region(x,y,width,height) / analyze_screen(question,capture_mode) / "
        "read_window(title) / inspect_window(title, include_screenshot, include_vision) "
        "[returns @eN refs valid across calls] / background_screenshot(title). "
        "Window control: launch_app(app_name, bring_to_front) / focus_window(title|hwnd) / wait(ms). "
        "Click — variants: click(x,y|name) / click_ref(ref, background, return_diff) / "
        "double_click(x,y) / right_click(x,y) / drag(start,end) / "
        "mouse_button_down(button) / mouse_button_up(button) / mouse_button_hold(button, duration_ms). "
        "Key press — variants: key(keys) / type(text, target_window, respect_focus) / "
        "background_type(text, target_window) / key_down(key) / key_up(key) / "
        "key_hold(key, duration_ms). "
        "Mouse motion — variants: mouse_move(x,y) / mouse_move_relative(dx,dy,duration_ms) / "
        "mouse_move_angle(degrees,axis,duration_ms) / scroll(direction,amount). "
        "System: volume(volume_action, level). "
        "Use browser_navigate only for URLs/web pages; use launch_app for any installed native desktop application. "
        "For pointer coordinates use the screenshot's logical 1024x768 coordinate plane unless coordinate_mode='physical'."
    ),
)
async def computer(
    action: Annotated[ComputerAction, Field(description="Computer action to execute.")],
    coordinate: Annotated[
        list[int] | None,
        Field(description="Logical [x, y] coordinate in 1024x768 space."),
    ] = None,
    x: Annotated[int | None, Field(description="Logical x coordinate for pointer or region actions.")] = None,
    y: Annotated[int | None, Field(description="Logical y coordinate for pointer or region actions.")] = None,
    width: Annotated[int | None, Field(description="Logical width for observe_region.")] = None,
    height: Annotated[int | None, Field(description="Logical height for observe_region.")] = None,
    end_coordinate: Annotated[
        list[int] | None,
        Field(description="Logical [x, y] drag destination in 1024x768 space."),
    ] = None,
    end_x: Annotated[int | None, Field(description="Logical drag destination x coordinate.")] = None,
    end_y: Annotated[int | None, Field(description="Logical drag destination y coordinate.")] = None,
    text: Annotated[str, Field(description="Text for the type action. Never logged raw.")] = "",
    target_window: Annotated[
        str, Field(description="Optional title substring to scope focus, click, type, or key.")
    ] = "",
    respect_focus: Annotated[
        bool,
        Field(description="For type, send characters to the resolved target hwnd where possible."),
    ] = True,
    key: Annotated[str, Field(description="Key or key chord for the key action, for example ctrl+s.")] = "",
    keys: Annotated[str, Field(description="Alias for key: normalized chord such as ctrl+s or windows+s.")] = "",
    button: Annotated[str, Field(description="Mouse button for click actions: left, right, or middle.")] = "left",
    name: Annotated[str, Field(description="Accessibility element name for click-by-name.")] = "",
    coordinate_mode: Annotated[
        str,
        Field(description="Coordinate plane for x/y. Use 'logical' by default; legacy aliases may pass 'physical'."),
    ] = "logical",
    direction: Annotated[str, Field(description="Scroll direction: up, down, left, or right.")] = "down",
    amount: Annotated[int, Field(description="Scroll amount when scroll_x/scroll_y are not supplied.")] = 5,
    scroll_x: Annotated[int, Field(description="Horizontal scroll delta.")] = 0,
    scroll_y: Annotated[int, Field(description="Vertical scroll delta.")] = 0,
    duration_ms: Annotated[int, Field(description="Wait or drag duration in milliseconds.")] = 1000,
    ms: Annotated[int | None, Field(description="Alias for duration_ms used by wait(ms).")] = None,
    monitor_id: Annotated[int | None, Field(description="Optional mss monitor index.")] = None,
    window_handle: Annotated[int | None, Field(description="Native HWND for focus_window.")] = None,
    hwnd: Annotated[int | None, Field(description="Alias for window_handle.")] = None,
    window_title: Annotated[str, Field(description="Title substring for focus_window/read_window/type.")] = "",
    title: Annotated[str, Field(description="Alias title substring for focus_window/read_window.")] = "",
    app_executable: Annotated[str, Field(description="Executable name for focus_window or approval context.")] = "",
    app_name: Annotated[
        str, Field(description="Native app name for launch_app (the application's display or executable name).")
    ] = "",
    bring_to_front: Annotated[bool, Field(description="For launch_app, focus the launched window when found.")] = True,
    include_minimized: Annotated[bool, Field(description="Include minimized windows in list_windows.")] = False,
    include_window_titles: Annotated[bool, Field(description="Return window titles in list_windows.")] = True,
    region: Annotated[
        str,
        Field(
            description="Screenshot target: empty/full for normalized screen, or window:<title>/title for legacy window capture."
        ),
    ] = "",
    volume_action: Annotated[
        str,
        Field(description="For volume action: get, set, mute, unmute, up, or down."),
    ] = "get",
    level: Annotated[int | None, Field(description="Target system volume percent for volume(set).")] = None,
    percent: Annotated[int | None, Field(description="Alias for level.")] = None,
    mute: Annotated[bool | None, Field(description="Optional mute state for volume(set).")] = None,
    step: Annotated[int, Field(description="Step size for volume(up/down).")] = 10,
    question: Annotated[str, Field(description="Question for analyze_screen.")] = "",
    capture_mode: Annotated[
        str,
        Field(description="Capture target for analyze_screen: active_window, full_screen, or cursor_region."),
    ] = "active_window",
    ref: Annotated[
        str,
        Field(
            description="Element ref like '@e5' from a recent inspect_window/read_window snapshot. Used by click_ref."
        ),
    ] = "",
    snapshot_id: Annotated[
        str,
        Field(description="Specific snapshot to resolve ref against. Defaults to the most recent."),
    ] = "",
    background: Annotated[
        bool,
        Field(
            description=(
                "Run the action without taking the user's mouse/keyboard. Click uses UIA Invoke only "
                "(refuses if unsupported); type uses send_chars to a target hwnd; screenshot uses "
                "PrintWindow API (works on minimized windows). Default false (foreground)."
            ),
        ),
    ] = False,
    include_screenshot: Annotated[
        bool,
        Field(description="For inspect_window: capture a screenshot alongside the UIA snapshot."),
    ] = True,
    include_vision: Annotated[
        bool,
        Field(description="For inspect_window: also run vision analysis with the supplied question."),
    ] = False,
    return_diff: Annotated[
        bool,
        Field(description="For click_ref: take a post-click snapshot and return state_diff."),
    ] = True,
    dx: Annotated[
        int,
        Field(description="For mouse_move_relative: horizontal raw mouse delta. Negative = left."),
    ] = 0,
    dy: Annotated[
        int,
        Field(description="For mouse_move_relative: vertical raw mouse delta. Negative = up."),
    ] = 0,
    degrees: Annotated[
        float,
        Field(description="For mouse_move_angle: rotation in degrees (~12 raw units per degree heuristic)."),
    ] = 0.0,
    axis: Annotated[
        str,
        Field(description="For mouse_move_angle: 'horizontal' (yaw) or 'vertical' (pitch)."),
    ] = "horizontal",
) -> str:
    """Execute one computer-use action."""
    started = time.monotonic()
    target_app = ""
    user_id = ""
    screenshot_hash_prefix = ""
    success = False
    control_token: safety.ComputerUseControlSession | None = None
    try:
        assert_desktop_surface()
        user_id = _get_call_user_id()
        session_id = _get_call_session_id(user_id)
        assert_local_user(user_id)
        safety.require_enabled(action, user_id=user_id)
        if action in _ACTIONS_WITHOUT_FOREGROUND_TARGET:
            target_app = safety.normalize_executable_name(app_executable or app_name)
        else:
            target_app = (
                safety.normalize_executable_name(app_executable) or window_manager.get_foreground_app_executable_name()
            )
            safety.require_not_uac(action, target_app_executable=target_app)
        chain_status = _next_chain_status(user_id)
        if chain_status.get("ok") is False:
            _release_held_low_level_input(user_id)
            return _json(chain_status)
        safety.require_action_consent(
            action,
            target_app_executable=target_app,
            user_id=user_id,
            session_id=session_id,
        )
        control_token = safety.acquire_control_session(user_id=user_id, session_id=session_id, action=action)

        if action == "launch_app":
            result = app_launcher.launch_app(app_name or title or window_title, bring_to_front=bring_to_front)
        elif action == "list_windows":
            result = window_manager.list_windows(
                include_minimized=include_minimized,
                include_titles=include_window_titles,
            )
        elif action == "screenshot":
            screenshot_region = region.strip()
            if screenshot_region and screenshot_region.lower() != "full":
                if screenshot_region.lower().startswith("window:"):
                    screenshot_region = screenshot_region.split(":", 1)[1].strip()
                result = await _desktop_screenshot(screenshot_region)
            else:
                result = screen.capture_screenshot(
                    monitor_id=monitor_id,
                    image_format=safety.get_screenshot_format(user_id=user_id),
                    quality=safety.get_screenshot_quality(user_id=user_id),
                )
                screenshot_hash_prefix = str(result.get("screenshot_hash_prefix", ""))
        elif action == "observe_region":
            if x is None or y is None or width is None or height is None:
                raise safety.ComputerUseRefusal(
                    error_category="COMPUTER_USE_INVALID_ARGUMENTS",
                    reason="x, y, width, and height are required for observe_region",
                    action=action,
                    target_app_executable=target_app,
                )
            result = screen.capture_region(
                x=int(x),
                y=int(y),
                width=int(width),
                height=int(height),
                monitor_id=monitor_id,
                image_format=safety.get_screenshot_format(user_id=user_id),
                quality=safety.get_screenshot_quality(user_id=user_id),
            )
            screenshot_hash_prefix = str(result.get("screenshot_hash_prefix", ""))
        elif action == "analyze_screen":
            result = await _analyze_screen(question, capture_mode)
        elif action == "focus_window":
            result = window_manager.focus_window(
                handle=window_handle if window_handle is not None else hwnd,
                title=_target_window_title(target_window=target_window, window_title=window_title, title=title) or None,
                app_executable=app_executable or None,
            )
            target_app = safety.normalize_executable_name(str(result.get("target_app_executable", target_app)))
        elif action == "read_window":
            result = await _desktop_read_window(
                _target_window_title(target_window=target_window, window_title=window_title, title=title)
            )
        elif action == "click":
            if target_window:
                refusal = _focus_precondition(window_manager.focus_window(title=target_window), action=action)
                if refusal is not None:
                    return _json(refusal)
            if name:
                result = await _desktop_click_name(
                    name,
                    _target_window_title(
                        target_window=target_window,
                        window_title=window_title,
                        title=title,
                    ),
                )
            elif coordinate_mode.strip().lower() == "physical":
                result = await _desktop_mouse_click(int(x or 0), int(y or 0), button)
            else:
                result = computer_input.click(
                    _coordinate_from_xy(action, coordinate, x, y),
                    button=button,
                    monitor_id=monitor_id,
                )
        elif action == "double_click":
            result = computer_input.click(
                _coordinate_from_xy(action, coordinate, x, y),
                double=True,
                button=button,
                monitor_id=monitor_id,
            )
        elif action == "right_click":
            result = computer_input.click(
                _coordinate_from_xy(action, coordinate, x, y),
                button="right",
                monitor_id=monitor_id,
            )
        elif action == "type":
            safety.require_not_password_field(action=action, target_app_executable=target_app)
            safety.require_text_allowed(text, action=action, target_app_executable=target_app, user_id=user_id)
            result = await _desktop_type(
                text,
                respect_focus=respect_focus,
                window_title=_target_window_title(
                    target_window=target_window,
                    window_title=window_title,
                    title=title,
                ),
            )
        elif action == "key":
            key_text = keys or key
            safety.require_key_allowed(key_text, action=action, target_app_executable=target_app)
            if target_window:
                refusal = _focus_precondition(window_manager.focus_window(title=target_window), action=action)
                if refusal is not None:
                    return _json(refusal)
            result = await _desktop_hotkey(key_text)
        elif action == "scroll":
            if target_window:
                refusal = _focus_precondition(window_manager.focus_window(title=target_window), action=action)
                if refusal is not None:
                    return _json(refusal)
            horizontal, vertical = _scroll_deltas(direction, amount, scroll_x, scroll_y)
            result = computer_input.scroll(
                coordinate=_coordinate_from_optional_xy(coordinate, x, y),
                scroll_x=horizontal,
                scroll_y=vertical,
                monitor_id=monitor_id,
            )
        elif action == "volume":
            result = await _desktop_volume(
                action=volume_action,
                percent=level if level is not None else percent,
                mute=mute,
                step=step,
            )
        elif action == "mouse_move":
            result = computer_input.move_mouse(_coordinate_from_xy(action, coordinate, x, y), monitor_id=monitor_id)
        elif action == "drag":
            resolved_end = end_coordinate
            if resolved_end is None and end_x is not None and end_y is not None:
                resolved_end = [int(end_x), int(end_y)]
            if resolved_end is None:
                raise safety.ComputerUseRefusal(
                    error_category="COMPUTER_USE_INVALID_ARGUMENTS",
                    reason="end_coordinate is required for drag",
                    action=action,
                    target_app_executable=target_app,
                )
            result = computer_input.drag(
                _coordinate_from_xy(action, coordinate, x, y),
                resolved_end,
                duration_ms=duration_ms,
                monitor_id=monitor_id,
            )
        elif action == "wait":
            result = computer_input.wait(duration_ms if ms is None else ms)
        elif action == "inspect_window":
            from intent.tools.desktop_refs import inspect_window_sync

            result = await asyncio.to_thread(
                inspect_window_sync,
                _target_window_title(target_window=target_window, window_title=window_title, title=title),
                include_screenshot=include_screenshot,
                include_vision=include_vision,
                vision_question=question,
                background=background,
            )
        elif action == "click_ref":
            if not ref:
                raise safety.ComputerUseRefusal(
                    error_category="COMPUTER_USE_INVALID_ARGUMENTS",
                    reason="ref is required for click_ref (e.g. '@e5' from a recent inspect_window)",
                    action=action,
                    target_app_executable=target_app,
                )
            from intent.tools.desktop_refs import click_ref_sync

            result = await asyncio.to_thread(
                click_ref_sync,
                ref,
                snapshot_id=snapshot_id or None,
                background=background,
                return_diff=return_diff,
            )
        elif action == "background_type":
            safety.require_not_password_field(action=action, target_app_executable=target_app)
            safety.require_text_allowed(text, action=action, target_app_executable=target_app, user_id=user_id)
            from intent.tools.desktop_refs import background_type_sync

            target_hwnd = window_handle if window_handle is not None else hwnd
            if target_hwnd is None:
                # Resolve hwnd from window title
                resolved_title = _target_window_title(
                    target_window=target_window, window_title=window_title, title=title
                )
                if resolved_title:
                    from intent.tools.desktop import _resolve_window

                    target_hwnd = int(_resolve_window(resolved_title).handle or 0)
            if not target_hwnd:
                raise safety.ComputerUseRefusal(
                    error_category="COMPUTER_USE_INVALID_ARGUMENTS",
                    reason="background_type requires window_handle, hwnd, or window_title.",
                    action=action,
                    target_app_executable=target_app,
                )
            result = await asyncio.to_thread(background_type_sync, text, int(target_hwnd))
        elif action == "background_screenshot":
            from intent.tools.desktop_refs import background_screenshot_sync

            result = await asyncio.to_thread(
                background_screenshot_sync,
                _target_window_title(target_window=target_window, window_title=window_title, title=title),
            )
        elif action == "mouse_move_relative":
            from intent.tools import desktop_input_low

            dispatch = await desktop_input_low.mouse_move_relative(int(dx), int(dy), int(duration_ms))
            result = _dispatch_envelope(
                dispatch,
                dx=int(dx),
                dy=int(dy),
                duration_ms=int(duration_ms),
            )
        elif action == "mouse_button_down":
            from intent.tools import desktop_input_low

            mouse_button = _normalize_low_level_mouse_button(
                button or "left",
                action=action,
                target_app_executable=target_app,
            )
            dispatch = desktop_input_low.mouse_button_down(mouse_button)
            # Track the press only when Windows took it. A refused press
            # that we recorded as held would make the chain's cleanup send
            # a button-up for a button that was never down.
            if getattr(dispatch, "submitted", True):
                _track_low_level_mouse_down(user_id, mouse_button)
            result = _dispatch_envelope(dispatch, button=mouse_button)
        elif action == "mouse_button_up":
            from intent.tools import desktop_input_low

            mouse_button = _normalize_low_level_mouse_button(
                button or "left",
                action=action,
                target_app_executable=target_app,
            )
            dispatch = desktop_input_low.mouse_button_up(mouse_button)
            # Untrack regardless: a refused release leaves the button held
            # in reality, but re-sending it on cleanup is the right move,
            # and the refusal is reported to the caller either way.
            _track_low_level_mouse_up(user_id, mouse_button)
            result = _dispatch_envelope(dispatch, button=mouse_button)
        elif action == "mouse_button_hold":
            from intent.tools import desktop_input_low

            mouse_button = _normalize_low_level_mouse_button(
                button or "left",
                action=action,
                target_app_executable=target_app,
            )
            dispatch = await desktop_input_low.mouse_button_hold(mouse_button, int(duration_ms))
            result = _dispatch_envelope(dispatch, button=mouse_button, duration_ms=int(duration_ms))
        elif action == "key_down":
            from intent.tools import desktop_input_low

            key_text = _required_key_argument(action, key or keys)
            safety.require_low_level_key_allowed(
                key_text,
                action=action,
                target_app_executable=target_app,
                user_id=user_id,
                session_id=session_id,
            )
            dispatch = desktop_input_low.key_down(key_text)
            # Only record a key as held when Windows actually took the press.
            # A refused press recorded as held both lies to the safety
            # chord-tracker and makes the chain cleanup release a key that
            # was never down.
            if getattr(dispatch, "submitted", True):
                safety.record_low_level_key_down(key_text, user_id=user_id, session_id=session_id)
                _track_low_level_key_down(user_id, key_text)
            result = _dispatch_envelope(dispatch, key=key_text)
        elif action == "key_up":
            from intent.tools import desktop_input_low

            key_text = _required_key_argument(action, key or keys)
            safety.require_low_level_key_allowed(
                key_text,
                action=action,
                target_app_executable=target_app,
                user_id=user_id,
                session_id=session_id,
            )
            dispatch = None
            try:
                dispatch = desktop_input_low.key_up(key_text)
            finally:
                safety.record_low_level_key_up(key_text, user_id=user_id, session_id=session_id)
                _track_low_level_key_up(user_id, key_text)
            result = _dispatch_envelope(dispatch, key=key_text)
        elif action == "key_hold":
            from intent.tools import desktop_input_low

            key_text = _required_key_argument(action, key or keys)
            safety.require_low_level_key_allowed(
                key_text,
                action=action,
                target_app_executable=target_app,
                user_id=user_id,
                session_id=session_id,
            )
            dispatch = await desktop_input_low.key_hold(key_text, int(duration_ms))
            result = _dispatch_envelope(dispatch, key=key_text, duration_ms=int(duration_ms))
        elif action == "mouse_move_angle":
            from intent.tools import desktop_input_low

            dispatch = await desktop_input_low.mouse_move_angle(float(degrees), axis or "horizontal", int(duration_ms))
            result = _dispatch_envelope(dispatch, degrees=float(degrees), axis=axis or "horizontal")
        else:
            raise safety.ComputerUseRefusal(
                error_category="COMPUTER_USE_INVALID_ACTION",
                reason="Unsupported computer action",
                action=action,
                target_app_executable=target_app,
            )

        success = bool(result.get("ok", True))
        duration = int((time.monotonic() - started) * 1000)
        merged = _merge_action_metadata(
            result,
            action=action,
            target_app_executable=target_app,
            duration_ms=duration,
            chain_status=chain_status,
            control_indicator=safety.control_session_indicator(control_token),
        )
        return _json(merged)
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        if user_id:
            _release_held_low_level_input(user_id)
        if isinstance(exc, (safety.ComputerUseRefusal, NotImplementedError, PermissionError)):
            logger.info(
                "computer_use_action action_type=%s target_app_executable=%s screenshot_hash_prefix=%s success=%s duration_ms=%d",
                action,
                target_app,
                screenshot_hash_prefix,
                False,
                duration,
            )
            return _json(_envelope_from_exception(exc))
        logger.exception("computer_use_action failed")
        return _json(_envelope_from_exception(exc))
    finally:
        safety.release_control_session(control_token)
        duration = int((time.monotonic() - started) * 1000)
        logger.info(
            "computer_use_action action_type=%s target_app_executable=%s screenshot_hash_prefix=%s success=%s duration_ms=%d",
            action,
            target_app,
            screenshot_hash_prefix,
            success,
            duration,
        )


@server.tool(
    name="desktop_volume",
    annotations=_CONFIRM,
    meta={"risk": "confirm", "anthropic/alwaysLoad": True},
    description=(
        "Compact machine-wide system output volume control for Windows/macOS/Linux. This changes the OS/desktop "
        "speaker output, not Viola's active music player volume, per-player media volume, microphone volume, "
        "or Viola preference defaults. This tool is first-turn native. Viola preference defaults such as "
        "default music volume, default playback volume, TTS volume, microphone volume, or speaker defaults "
        "belong in user_settings."
    ),
)
async def desktop_volume(
    action: Annotated[
        Literal["get", "set", "mute", "unmute", "up", "down"],
        Field(description="Volume action: get, set, mute, unmute, up, or down."),
    ] = "get",
    percent: Annotated[int | None, Field(description="Target system volume percent for action='set'.")] = None,
    mute: Annotated[bool | None, Field(description="Optional mute state when setting volume.")] = None,
    step: Annotated[int, Field(description="Percent step size for action='up' or action='down'.")] = 10,
) -> str:
    """Read or change the machine-wide system output volume via the computer-use safety path."""
    return await computer(
        action="volume",
        volume_action=action,
        level=percent,
        mute=mute,
        step=step,
    )


def create_computer_use_server() -> FastMCP:
    """Return the FastMCP server instance for in-process registration."""
    return server


if __name__ == "__main__":
    from services.sentry_init import init_sentry

    init_sentry("mcp_servers.computer_use.server")
    server.run()
