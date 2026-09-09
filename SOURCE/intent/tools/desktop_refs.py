"""Ref-based desktop computer-use primitives.

Adds the four architectural pieces missing from `intent/tools/desktop.py`:

  1. **Refs across calls** — `read_window` / `inspect_window` produce stable
     `@e1`, `@e2` refs anchored to a snapshot. `click_ref(@e5)` clicks the
     exact element from that snapshot. No more re-walking the UIA tree on
     every click; no more name-based ambiguity that caused the phantom
     Document click on Steam (trace d487101b6647 step 8).

  2. **Single observation primitive** — `inspect_window` combines UIA tree
     (with refs) + optional screenshot + optional vision-analysis into ONE
     tool result. Replaces the focus_window + read_window + analyze_screen
     triple that costs 5-8s and ~20K tokens per agent decision.

  3. **Post-click semantic diff** — every click via ref returns
     `state_diff` (added/removed/changed elements) computed against the
     pre-click snapshot. Agent gets actionable feedback without an extra
     observe call.

  4. **Background mode** — `background=True` on click/type/screenshot uses
     UIA Invoke / send_chars / PrintWindow respectively. Doesn't move the
     user's cursor or steal keyboard focus when the target supports the
     non-input path. Returns a clear error if the target requires
     foreground (e.g. CEF Link without Invoke pattern).

This module is pure-additive and doesn't touch the legacy `click(name=)` /
`type(text)` paths in `desktop.py` — they remain available for callers
that haven't migrated.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Snapshot model
# ---------------------------------------------------------------------------


@dataclass
class ElementSnapshot:
    """One UIA element captured at snapshot time."""

    ref: str  # "@e1", "@e2", ...
    name: str
    control_type: str
    rect: tuple[int, int, int, int]  # (left, top, right, bottom) physical pixels
    enabled: bool
    runtime_id: tuple[int, ...]  # UIA stable identifier across calls
    parent_ref: str | None = None

    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "name": self.name,
            "type": self.control_type,
            "enabled": self.enabled,
            "rect": list(self.rect),
            "parent_ref": self.parent_ref,
        }


@dataclass
class WindowSnapshot:
    """A frozen view of one window's UIA tree."""

    snapshot_id: str
    window_title: str
    window_hwnd: int
    captured_at: float
    elements: list[ElementSnapshot] = field(default_factory=list)

    def by_ref(self, ref: str) -> ElementSnapshot | None:
        for elem in self.elements:
            if elem.ref == ref:
                return elem
        return None


# ---------------------------------------------------------------------------
# Snapshot cache (process-local)
# ---------------------------------------------------------------------------

_SNAPSHOT_TTL_SECONDS = 600  # 10 minutes
_SNAPSHOT_CACHE_MAX = 16  # keep last N snapshots
_snapshot_cache: dict[str, WindowSnapshot] = {}
_latest_snapshot_id: str | None = None


def _expire_old_snapshots() -> None:
    """Drop snapshots beyond TTL or cache cap."""
    global _snapshot_cache
    now = time.time()
    fresh = {sid: snap for sid, snap in _snapshot_cache.items() if now - snap.captured_at < _SNAPSHOT_TTL_SECONDS}
    if len(fresh) > _SNAPSHOT_CACHE_MAX:
        # Keep most recent N by captured_at
        items = sorted(fresh.items(), key=lambda kv: kv[1].captured_at, reverse=True)
        fresh = dict(items[:_SNAPSHOT_CACHE_MAX])
    _snapshot_cache = fresh


def get_snapshot(snapshot_id: str | None) -> WindowSnapshot | None:
    """Return the named snapshot, or the most recent one if id is None."""
    _expire_old_snapshots()
    if snapshot_id is None:
        if _latest_snapshot_id is None:
            return None
        return _snapshot_cache.get(_latest_snapshot_id)
    return _snapshot_cache.get(snapshot_id)


def store_snapshot(snap: WindowSnapshot) -> None:
    global _latest_snapshot_id
    _snapshot_cache[snap.snapshot_id] = snap
    _latest_snapshot_id = snap.snapshot_id
    _expire_old_snapshots()


def reset_cache_for_tests() -> None:
    global _snapshot_cache, _latest_snapshot_id
    _snapshot_cache = {}
    _latest_snapshot_id = None


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------


def _resolve_target_hwnd(window_title: str = "") -> int:
    import ctypes

    if window_title and window_title.strip():
        from intent.tools.desktop import _resolve_window

        return int(_resolve_window(window_title).handle or 0)
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _resolve_target_hwnd_fast(window_title: str = "") -> int:
    """Resolve only through Win32 APIs so this never pays UIA enumeration cost."""
    import ctypes

    if window_title and window_title.strip():
        try:
            from services.computer_use.window_manager import _resolve_hwnd_fast

            return int(_resolve_hwnd_fast(window_title) or 0)
        except Exception as exc:
            logger.debug("Fast hwnd resolve failed for %r: %s", window_title, exc)
            return 0
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


_UIA_WALK_TIMEOUT_S = 5.0
"""Cap on how long we wait for the entire pywinauto + UIA chain to produce
a usable snapshot.

Trace e07d914d762e step 4 spent 27 seconds inspecting a Minecraft Java window
that has no UIA tree (game canvas exposes nothing). The slowness was NOT
``wrapper.descendants()`` alone — ``Application(backend='uia').connect()`` and
``wrapper_object()`` themselves can each block for many seconds against an
unresponsive UIA backend. So we wrap the whole chain (connect, resolve, walk)
in one timeout instead of just the descendants step.
"""


def capture_window_snapshot(window_title: str = "", *, max_elements: int = 300) -> WindowSnapshot:
    """Walk the UIA tree of the target window and freeze it into a snapshot.

    The entire pywinauto+UIA chain runs in a daemon thread bounded by
    ``_UIA_WALK_TIMEOUT_S``. If the timeout fires we return an empty snapshot
    (with the resolved hwnd) and let the caller fall back to vision.
    """
    # Cheap Win32 resolve happens before the worker; any slow UIA fallback
    # resolve stays inside the same timeout as connect/wrapper/walk.
    hwnd = _resolve_target_hwnd_fast(window_title)

    result_holder: dict[str, Any] = {"title": "", "elements": [], "hwnd": hwnd}
    error_holder: list[Exception] = []

    def _build_snapshot() -> None:
        try:
            from pywinauto import Application

            target_hwnd = int(result_holder["hwnd"] or 0)
            if not target_hwnd:
                target_hwnd = _resolve_target_hwnd(window_title)
                result_holder["hwnd"] = target_hwnd
            app = Application(backend="uia").connect(handle=target_hwnd)
            window_spec = app.window(handle=target_hwnd)
            wrapper = window_spec.wrapper_object()
            result_holder["title"] = wrapper.window_text()
            descendants = list(wrapper.descendants())[:max_elements]
            built: list[ElementSnapshot] = []
            for i, child in enumerate(descendants):
                try:
                    ctrl_type = str(getattr(child.element_info, "control_type", "") or "")
                    rect_obj = child.rectangle()
                    rect = (int(rect_obj.left), int(rect_obj.top), int(rect_obj.right), int(rect_obj.bottom))
                    runtime_id_raw = getattr(child.element_info, "runtime_id", None) or ()
                    runtime_id = tuple(int(x) for x in runtime_id_raw) if runtime_id_raw else ()
                    built.append(
                        ElementSnapshot(
                            ref="@e%d" % (i + 1),
                            name=child.window_text() or "",
                            control_type=ctrl_type,
                            rect=rect,
                            enabled=bool(child.is_enabled()),
                            runtime_id=runtime_id,
                        )
                    )
                except Exception as exc:
                    logger.debug("Skipping descendant %d during snapshot: %s", i, exc)
            result_holder["elements"] = built
        except Exception as exc:
            error_holder.append(exc)

    walker = threading.Thread(target=_build_snapshot, daemon=True, name="uia-snapshot-build")
    walker.start()
    walker.join(timeout=_UIA_WALK_TIMEOUT_S)
    if walker.is_alive():
        logger.warning(
            "UIA snapshot build exceeded %.1fs for hwnd=%s; returning empty snapshot",
            _UIA_WALK_TIMEOUT_S,
            result_holder["hwnd"] or hwnd,
        )
    if error_holder and not result_holder["elements"] and not walker.is_alive() and not result_holder["hwnd"]:
        raise error_holder[0]
    if error_holder and not result_holder["elements"]:
        logger.debug("UIA snapshot build error for hwnd=%s: %s", result_holder["hwnd"] or hwnd, error_holder[0])

    title = result_holder["title"] or window_title or ""
    elements: list[ElementSnapshot] = result_holder["elements"]
    hwnd = int(result_holder["hwnd"] or hwnd or 0)

    snapshot_id = "snap_%s" % uuid.uuid4().hex[:12]
    snap = WindowSnapshot(
        snapshot_id=snapshot_id,
        window_title=title,
        window_hwnd=hwnd,
        captured_at=time.time(),
        elements=elements,
    )
    store_snapshot(snap)
    return snap


# ---------------------------------------------------------------------------
# Background-mode helpers
# ---------------------------------------------------------------------------


def _try_uia_invoke(snapshot: WindowSnapshot, ref: str) -> bool:
    """Attempt UIA InvokePattern on the element. Returns True on success.

    Background-mode click path: doesn't move the cursor, doesn't steal
    keyboard focus. Only works for elements that implement InvokePattern
    (most native Buttons, Menus, Hyperlinks). Returns False for anything
    that doesn't support Invoke (CEF custom controls, raw Documents).
    """
    elem_snapshot = snapshot.by_ref(ref)
    if elem_snapshot is None:
        return False
    try:
        # Re-resolve the live UIA element by runtime_id to invoke it.
        from pywinauto import Application

        app = Application(backend="uia").connect(handle=snapshot.window_hwnd)
        window = app.window(handle=snapshot.window_hwnd)
        wrapper = window.wrapper_object()
        for child in wrapper.descendants():
            try:
                child_runtime = tuple(getattr(child.element_info, "runtime_id", None) or ())
                if child_runtime == elem_snapshot.runtime_id:
                    child.invoke()
                    return True
            except Exception:
                continue
    except Exception as exc:
        logger.debug("UIA invoke failed for %s: %s", ref, exc)
    return False


def _send_chars_to_hwnd(text: str, hwnd: int) -> bool:
    """Type text into a specific HWND without focus-stealing."""
    if not hwnd:
        return False
    try:
        from intent.tools.desktop import _hwnd_wrapper

        wrapper = _hwnd_wrapper(hwnd)
        wrapper.send_chars(text, with_spaces=True)
        return True
    except Exception as exc:
        logger.debug("send_chars to hwnd %d failed: %s", hwnd, exc)
        return False


def _printwindow_screenshot(hwnd: int) -> bytes | None:
    """Capture a window's bitmap via PrintWindow API.

    Works on minimized/occluded windows and doesn't disturb what's on the
    user's visible screen. Returns PNG bytes or None on failure.
    """
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        from io import BytesIO

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # Get window dimensions
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None

        # Capture
        hdc_window = user32.GetDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbm = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        old_bmp = gdi32.SelectObject(hdc_mem, hbm)

        # PW_CLIENTONLY=1 | PW_RENDERFULLCONTENT=2 = 3
        result = user32.PrintWindow(hwnd, hdc_mem, 3)

        if not result:
            gdi32.SelectObject(hdc_mem, old_bmp)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)
            return None

        # Convert HBITMAP -> PNG via PIL
        # Read pixels via a small ctypes-based DIB read. This MUST happen
        # before the window DC is released and before the memory DC/bitmap
        # are torn down — GetDIBits needs a live device context and a bitmap
        # still holding PrintWindow's pixels. Pre-fix, GetDIBits ran AFTER
        # ReleaseDC/DeleteDC below, so it silently failed against an already-
        # released DC, leaving `buf` zero-filled and returning an all-black
        # frame as a successful capture (#2773 item 1).
        import struct

        from PIL import Image

        BI_RGB = 0
        bmiheader_size = 40
        bmi = ctypes.create_string_buffer(bmiheader_size)
        struct.pack_into("<IiiHHIIiiII", bmi, 0, bmiheader_size, width, -height, 1, 32, BI_RGB, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        rows_copied = gdi32.GetDIBits(hdc_window, hbm, 0, height, buf, bmi, 0)

        # Now safe to tear down GDI objects: select the original bitmap back
        # out of hdc_mem before deleting it, then release the window DC.
        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)

        if not rows_copied:
            logger.warning(
                "GetDIBits returned 0 rows for hwnd=%d; failing closed instead of returning a blank frame", hwnd
            )
            return None

        img = Image.frombuffer("RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1)
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.exception("PrintWindow capture failed for hwnd=%d", hwnd)
        return None


# ---------------------------------------------------------------------------
# click_ref + state diff
# ---------------------------------------------------------------------------


def _diff_is_empty(diff: dict[str, Any] | None) -> bool:
    """True when a post-click snapshot showed the window did not change."""
    if not isinstance(diff, dict):
        return True
    return not (diff.get("added") or diff.get("removed") or diff.get("changed"))


def _apply_click_evidence(result: dict[str, Any], before: WindowSnapshot, *, method: str) -> None:
    """Re-snapshot the target and let the diff decide what the result claims.

    This is the one place in the desktop-input family that has a real
    observer of the target rather than of the injection boundary, and it
    was already being computed and then ignored: ``state_diff`` went into
    the payload as decoration while ``ok: True`` was asserted regardless.
    So a click that provably changed nothing in the window read exactly
    like a click that opened a dialog.

    A UIA tree that changed is evidence the click did something. An
    unchanged tree is not proof of failure -- plenty of real clicks change
    no accessibility state (typing focus, a canvas repaint, a background
    request) -- so it downgrades to unverified rather than to a failure we
    cannot prove. A snapshot we could not take is unverified too, instead
    of being swallowed by a bare ``logger.exception``.
    """
    try:
        after = capture_window_snapshot(before.window_title)
    except Exception:
        logger.exception("post-click snapshot failed")
        result["unverified"] = True
        result["unverified_reason"] = (
            "the click was submitted but the post-click snapshot failed, so nothing observed whether the window reacted"
        )
        return

    if before.elements and not after.elements:
        # capture_window_snapshot returns an EMPTY snapshot when the UIA walk
        # times out (5s cap; a Minecraft window once burned 27 seconds with no
        # tree at all). Diffing against it would show every element as
        # "removed" and read as a big observed change -- a confirmation
        # manufactured out of a failed observation, which is the exact bug
        # class this whole change exists to remove.
        logger.warning("post-click snapshot came back empty for %r; treating as unobserved", before.window_title)
        result["unverified"] = True
        result["unverified_reason"] = (
            "the click was submitted but the post-click snapshot returned no elements (the accessibility "
            "walk timed out or the window exposes no tree), so nothing observed whether it took effect"
        )
        return

    diff = _diff_snapshots(before, after)
    result["state_diff"] = diff
    result["new_snapshot_id"] = after.snapshot_id
    if _diff_is_empty(diff):
        result["unverified"] = True
        result["unverified_reason"] = (
            "the click was submitted via %s and the window's accessibility tree is unchanged, so nothing "
            "observed it take effect (some real clicks change no tree state, so this is not proof it "
            "failed)" % method
        )
    else:
        # The window's own tree moved after the click. That is target-side
        # evidence, not an echo of the request.
        result["observed_change"] = True


def _diff_snapshots(before: WindowSnapshot, after: WindowSnapshot) -> dict[str, Any]:
    """Return added/removed/changed elements between two snapshots.

    Diff is computed on (control_type, name, runtime_id) — runtime_id is the
    stable UIA identity. Refs are local to each snapshot so we report names.
    """
    before_keys = {(e.runtime_id or e.ref, e.control_type, e.name): e for e in before.elements}
    after_keys = {(e.runtime_id or e.ref, e.control_type, e.name): e for e in after.elements}

    added = [e.to_dict() for k, e in after_keys.items() if k not in before_keys]
    removed = [e.to_dict() for k, e in before_keys.items() if k not in after_keys]

    # "changed" = same identity but different rect/enabled
    changed: list[dict[str, Any]] = []
    for k, e_before in before_keys.items():
        e_after = after_keys.get(k)
        if e_after is None:
            continue
        if e_before.rect != e_after.rect or e_before.enabled != e_after.enabled:
            changed.append(
                {
                    "ref": e_after.ref,
                    "name": e_after.name,
                    "type": e_after.control_type,
                    "rect_before": list(e_before.rect),
                    "rect_after": list(e_after.rect),
                    "enabled_before": e_before.enabled,
                    "enabled_after": e_after.enabled,
                }
            )

    return {"added": added[:50], "removed": removed[:50], "changed": changed[:50]}


def click_ref_sync(
    ref: str,
    *,
    snapshot_id: str | None = None,
    background: bool = False,
    return_diff: bool = True,
) -> dict[str, Any]:
    """Click a previously-snapshotted element by ref.

    Args:
        ref: '@e5'-style identifier from a recent read_window / inspect_window.
        snapshot_id: explicit snapshot. Defaults to latest.
        background: when True, only attempt UIA Invoke. Refuse if unsupported
            (no cursor movement, no focus steal, no real mouse).
        return_diff: when True, take a post-click snapshot and include
            state_diff in the result. Set False to skip the second walk.
    """
    snap = get_snapshot(snapshot_id)
    if snap is None:
        # No `ok` key here would be read as ok=True by the computer_use server
        # (result.get("ok", True)) and reported to the model as a successful
        # click that never happened. Fail closed.
        return {"ok": False, "error": "No snapshot available; call inspect_window or read_window first.", "ref": ref}

    elem = snap.by_ref(ref)
    if elem is None:
        return {
            "ok": False,
            "error": "ref %r not found in snapshot %s" % (ref, snap.snapshot_id),
            "ref": ref,
        }

    # Background mode: UIA Invoke only.
    if background:
        if _try_uia_invoke(snap, ref):
            # This dict carried NO ``ok`` key at all, and the computer_use
            # server reads a missing ``ok`` as True (result.get("ok", True)),
            # so the envelope asserted success by omission. State it.
            result = {
                "ok": True,
                "clicked": elem.name,
                "ref": ref,
                "method": "uia_invoke_background",
                "control_type": elem.control_type,
                "rect": list(elem.rect),
            }
            if return_diff:
                _apply_click_evidence(result, snap, method="uia_invoke_background")
            else:
                result["unverified"] = True
                result["unverified_reason"] = (
                    "return_diff was off, so no snapshot was taken and nothing observed the window react"
                )
            return result
        return {
            "ok": False,
            "error": "background mode unavailable for %s — element does not support InvokePattern. "
            "Try background=False or click(coordinate=[%d,%d])." % (elem.control_type, *elem.center()),
            "ref": ref,
            "method": "background_invoke_unsupported",
        }

    # Foreground path: try Invoke first (silent), then click_input, then rect-coord.
    method: str | None = None
    if _try_uia_invoke(snap, ref):
        method = "uia_invoke_%s" % elem.control_type
    else:
        # Try click_input via re-resolved live element.
        try:
            from pywinauto import Application

            app = Application(backend="uia").connect(handle=snap.window_hwnd)
            window = app.window(handle=snap.window_hwnd)
            wrapper = window.wrapper_object()
            for child in wrapper.descendants():
                try:
                    child_runtime = tuple(getattr(child.element_info, "runtime_id", None) or ())
                    if child_runtime == elem.runtime_id:
                        child.click_input()
                        method = "accessibility_%s" % elem.control_type
                        break
                except Exception:
                    continue
        except Exception:
            logger.debug("click_input via re-resolved element failed")

    if method is None:
        # Rect-coordinate fallback
        try:
            from intent.tools.desktop import _PYAUTOGUI_AVAILABLE, _PYWINAUTO_AVAILABLE, _pywinauto_mouse_module

            cx, cy = elem.center()
            if _PYWINAUTO_AVAILABLE:
                _pywinauto_mouse_module().click(button="left", coords=(cx, cy))
                method = "rect_coord_fallback_%s" % elem.control_type
            elif _PYAUTOGUI_AVAILABLE:
                import pyautogui

                pyautogui.click(cx, cy)
                method = "rect_coord_fallback_%s" % elem.control_type
        except Exception as exc:
            return {
                "ok": False,
                "error": "All click paths failed for %s: %s" % (ref, exc),
                "ref": ref,
            }

    if method is None:
        return {"ok": False, "error": "No click method succeeded", "ref": ref}

    result: dict[str, Any] = {
        "ok": True,
        "clicked": elem.name,
        "ref": ref,
        "method": method,
        "control_type": elem.control_type,
        "rect": list(elem.rect),
    }
    if return_diff:
        _apply_click_evidence(result, snap, method=method)
    else:
        # Caller turned off the only observer this path has. Say so rather
        # than let the absence of a check read as a passed check.
        result["unverified"] = True
        result["unverified_reason"] = (
            "return_diff was off, so no post-click snapshot was taken and nothing observed the window react"
        )
    return result


# ---------------------------------------------------------------------------
# inspect_window — single observation primitive
# ---------------------------------------------------------------------------


def inspect_window_sync(
    title: str = "",
    *,
    include_screenshot: bool = True,
    include_vision: bool = False,
    vision_question: str = "",
    background: bool = False,
) -> dict[str, Any]:
    """Combined snapshot + screenshot + (optional) vision in one tool result.

    Replaces the focus_window + read_window + analyze_screen triple the
    agent was doing per decision. Returns a snapshot_id usable with
    click_ref(...) and a refs-tagged element list.
    """
    snap = capture_window_snapshot(title)
    out: dict[str, Any] = {
        "snapshot_id": snap.snapshot_id,
        "window_title": snap.window_title,
        "window_hwnd": snap.window_hwnd,
        "elements": [e.to_dict() for e in snap.elements],
        "count": len(snap.elements),
    }
    if not snap.elements:
        # capture_window_snapshot returns an empty snapshot both when the
        # window genuinely exposes no accessibility tree (a game canvas) and
        # when the walk hit its timeout. "count: 0" alone reads as the
        # former, so say that the observation may simply not have completed.
        out["elements_incomplete"] = True
        out["elements_incomplete_reason"] = (
            "no accessibility elements were captured; the window may expose no tree, or the walk may "
            "have hit its timeout. Treat this as 'not observed', not as 'the window is empty'."
        )
    if include_screenshot:
        try:
            if background:
                png = _printwindow_screenshot(snap.window_hwnd)
                if png is None:
                    out["screenshot_error"] = "PrintWindow capture returned nothing for this window"
                else:
                    import base64

                    out["screenshot_b64"] = base64.b64encode(png).decode("ascii")
                    out["screenshot_mode"] = "printwindow_background"
            else:
                from intent.tools.desktop import _screenshot_sync

                shot = _screenshot_sync("full")
                out["screenshot_path"] = shot.get("path")
                out["screenshot_mode"] = "mss_foreground"
        except Exception:
            # Swallowing this left a result that asked for a screenshot,
            # carried none, and said nothing about why -- indistinguishable
            # from a caller that never asked.
            logger.exception("inspect_window screenshot failed")
            out["screenshot_error"] = "the screenshot could not be captured"

    if include_vision and vision_question:
        try:
            import asyncio

            from intent.tools.vision_tools import analyze_screen

            result = asyncio.run(analyze_screen(question=vision_question, capture_mode="active_window"))
            if isinstance(result, dict) and result.get("success"):
                out["vision_analysis"] = result.get("message")
        except Exception:
            logger.exception("inspect_window vision analysis failed")

    return out


# ---------------------------------------------------------------------------
# Background type / background screenshot tool entrypoints
# ---------------------------------------------------------------------------


def background_type_sync(text: str, hwnd: int) -> dict[str, Any]:
    """Type text into a target HWND without focus-stealing.

    ``_send_chars_to_hwnd`` returns True when pywinauto's ``send_chars``
    did not raise, which means the WM_CHAR messages were posted -- not that
    the window did anything with them. pywinauto discards SendMessage's
    reply, and a window with no focused edit control accepts and ignores
    WM_CHAR without complaint. So ``typed`` here is the length of what we
    asked for, and the result says as much.
    """
    if _send_chars_to_hwnd(text, hwnd):
        return {
            "ok": True,
            "typed": len(text),
            "method": "send_chars_background",
            "hwnd": hwnd,
            "unverified": True,
            "unverified_reason": (
                "WM_CHAR messages were posted to the window, but nothing observed the application accept "
                "them; a window with no focused text field ignores them silently"
            ),
        }
    return {
        "ok": False,
        "error": "background type failed — target HWND %d may not accept WM_CHAR. Try foreground type." % hwnd,
        "hwnd": hwnd,
    }


def background_screenshot_sync(window_title: str = "") -> dict[str, Any]:
    """PrintWindow capture — works on minimized/occluded windows."""
    hwnd = _resolve_target_hwnd(window_title)
    png = _printwindow_screenshot(hwnd)
    if png is None:
        return {"ok": False, "error": "PrintWindow capture failed for hwnd %d" % hwnd, "hwnd": hwnd}
    import base64

    return {
        "ok": True,
        "screenshot_b64": base64.b64encode(png).decode("ascii"),
        "method": "printwindow_background",
        "hwnd": hwnd,
        "size_bytes": len(png),
    }
