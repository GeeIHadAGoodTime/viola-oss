"""Owner-operated "Coming soon" switches for desktop features.

The owner can switch a desktop feature off from the cloud, with no new desktop
build, when it fails testing. While a feature is off the desktop:

1. shows "Coming soon" next to its option in Settings and disables the controls;
2. leaves its tools out of the tool list the model gets (and refuses a call to
   one of them that arrives anyway);
3. adds one short line telling the model the feature is coming soon.

Every feature is ON by default. With nothing switched off, none of this adds
anything: no tool is withheld, no prompt text is added, the settings payload is
unchanged. Gating is by feature key and tool name only, never by what the user
said.

The switches live in the cloud's launch kill-switch store
(``backend/launch_kill_switches.py``) and are flipped with its admin route.
``phone`` is the existing cloud phone kill-switch; every other key here is a
flag-only kill-switch that no cloud route serves.

The desktop reads the list of switched-off keys at launch, at sign-in and then
every 15 minutes, and keeps the last good answer on disk. It fails open: when the
cloud cannot be reached, answers 404 (an older cloud) or sends something it
cannot read, nothing is newly switched off; the cached list stays in force, or
nothing is off when there is no cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwitchableFeature:
    """One feature the owner can switch to "Coming soon"."""

    key: str
    """Stable key. It is also the cloud kill-switch name."""
    label: str
    """Plain name the user and the model read."""
    settings_options: tuple[str, ...]
    """Settings option ids (``ComingSoon option=...`` in the React settings)."""
    tools: tuple[str, ...]
    """Tool names withheld from the model while the feature is off."""


# The one registry. Tool names are the registered MCP tool names
# (mcp_servers/core_tools, mcp_servers/browser, mcp_servers/computer_use).
FEATURES: tuple[SwitchableFeature, ...] = (
    SwitchableFeature(
        key="phone",
        label="Phone calls",
        settings_options=("account.phone_calls",),
        tools=("phone", "transmit_payment_to_call"),
    ),
    SwitchableFeature(
        key="saved_card_purchases",
        label="Purchases with a saved card",
        settings_options=("account.payment_methods",),
        tools=("payment", "fill_payment_details", "resume_payment_gate", "transmit_payment_to_call"),
    ),
    SwitchableFeature(
        key="email_sending",
        label="Sending email",
        settings_options=("ai_agents.capability.email",),
        tools=("share_response",),
    ),
    SwitchableFeature(
        key="desktop_control",
        label="Desktop control",
        settings_options=("ai_agents.capability.desktop_control",),
        tools=("computer", "desktop_interact"),
    ),
    SwitchableFeature(
        key="desktop_browser",
        label="The agent browser",
        settings_options=("ai_agents.capability.browser",),
        tools=(
            "browser_back",
            "browser_close",
            "browser_fill_form",
            "browser_forward",
            "browser_get_api_log",
            "browser_get_text",
            "browser_interact",
            "browser_navigate",
            "browser_refresh",
            "browser_run_script",
            "browser_screenshot",
            "browser_scroll",
            "browser_snapshot",
            "browser_status",
            "browser_wait",
            "fill_payment_details",
            "verify_state",
        ),
    ),
    SwitchableFeature(
        key="custom_wake_word",
        label="Custom wake word training",
        settings_options=("customize.wake_word_training",),
        tools=(),
    ),
    SwitchableFeature(
        key="spotify",
        label="Spotify",
        settings_options=("music.source.spotify",),
        tools=(),
    ),
    SwitchableFeature(
        key="rooms",
        label="Rooms (music in other rooms)",
        settings_options=("system.lan_rooms",),
        tools=("pair_speaker_setup",),
    ),
    SwitchableFeature(
        key="smart_home",
        label="Smart home",
        settings_options=("connections.smart_home",),
        tools=("smart_home",),
    ),
    SwitchableFeature(
        key="background_agents",
        label="Background agents",
        settings_options=(),
        tools=("start_agent", "send_message"),
    ),
)

FEATURES_BY_KEY: dict[str, SwitchableFeature] = {feature.key: feature for feature in FEATURES}

# Keys that already are cloud kill-switches serving a cloud route. Every other
# key is registered in the cloud as a flag only.
CLOUD_SERVED_KEYS: frozenset[str] = frozenset({"phone"})

# The signed-in desktop's read endpoint (backend/cloud_app.py).
ENDPOINT_PATH = "/api/v1/cloud/features/coming-soon"
REFRESH_SECONDS = 15 * 60
FETCH_TIMEOUT_SECONDS = 10.0
CACHE_FILE_NAME = "feature_switches.json"

_EMPTY: frozenset[str] = frozenset()

_lock = threading.Lock()
_off: frozenset[str] = _EMPTY
_withheld_tools: frozenset[str] = _EMPTY


# ---------------------------------------------------------------------------
# Current state (read on the turn path; each read is one global lookup)
# ---------------------------------------------------------------------------


def switched_off() -> frozenset[str]:
    """Keys switched off right now (empty by default)."""
    return _off


def withheld_tool_names() -> frozenset[str]:
    """Tool names left out of the model's tool list right now."""
    return _withheld_tools


def feature_for_tool(tool_name: str) -> SwitchableFeature | None:
    """The switched-off feature that withholds *tool_name*, if any."""
    off = _off
    if not off:
        return None
    for key in sorted(off):
        feature = FEATURES_BY_KEY.get(key)
        if feature is not None and tool_name in feature.tools:
            return feature
    return None


def coming_soon_settings_options() -> list[str]:
    """Settings option ids to mark "Coming soon" (empty list by default)."""
    off = _off
    if not off:
        return []
    return sorted({option for key in off for option in FEATURES_BY_KEY[key].settings_options})


def coming_soon_line() -> str:
    """The one line for the model; empty when nothing is switched off."""
    off = _off
    if not off:
        return ""
    labels = "; ".join(FEATURES_BY_KEY[key].label for key in sorted(off))
    return (
        "Coming soon (switched off for now): %s. If the user asks for one of these, "
        "say plainly that it is coming soon instead of trying another way." % labels
    )


def coming_soon_tool_result(tool_name: str) -> dict[str, Any] | None:
    """A plain refusal for a call to a withheld tool, or None when it is not withheld."""
    if tool_name not in _withheld_tools:
        return None
    feature = feature_for_tool(tool_name)
    label = feature.label if feature is not None else "This feature"
    return {
        "success": False,
        "data": None,
        "display": "",
        "error": "%s is coming soon. It is switched off for now." % label,
        "error_category": "COMING_SOON",
        "blocked_tool": tool_name,
        "retryable": False,
    }


def apply_off(keys: Iterable[str]) -> frozenset[str]:
    """Set the switched-off keys (unknown keys are ignored). Returns the new set."""
    global _off, _withheld_tools
    known = frozenset(str(key) for key in keys if str(key) in FEATURES_BY_KEY)
    tools = frozenset(tool for key in known for tool in FEATURES_BY_KEY[key].tools) if known else _EMPTY
    with _lock:
        changed = known != _off
        _off = known if known else _EMPTY
        _withheld_tools = tools
    if changed:
        logger.info("Coming-soon features now: %s", sorted(known) or "none")
    return _off


def parse_off_payload(payload: Any) -> frozenset[str] | None:
    """Read ``{"off": [key, ...]}``. Returns None when it cannot be read."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("off")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return None
    return frozenset(item for item in raw if item in FEATURES_BY_KEY)


# ---------------------------------------------------------------------------
# Desktop cache and refresh
# ---------------------------------------------------------------------------


def cache_path() -> Path:
    from core.platform import get_data_dir

    return get_data_dir() / CACHE_FILE_NAME


def load_cache(path: Path | None = None) -> frozenset[str] | None:
    """The last good answer saved on disk, or None when there is none."""
    target = path or cache_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Coming-soon cache unreadable; ignoring it")
        return None
    return parse_off_payload(payload)


def save_cache(keys: Iterable[str], path: Path | None = None) -> None:
    target = path or cache_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"off": sorted(keys)}), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        logger.warning("Could not save the coming-soon cache", exc_info=True)


def _cloud_base_url() -> str:
    from config.settings import settings

    return str(getattr(settings, "cloud_url", "") or "https://api.useviola.com").rstrip("/")


async def _desktop_access_token() -> str | None:
    try:
        from auth.desktop_session import desktop_access_token_for_active_session

        return await desktop_access_token_for_active_session()
    except Exception:  # noqa: BLE001, RUF100 - no token means "keep what we have"
        logger.debug("Coming-soon refresh: no desktop access token", exc_info=True)
        return None


async def _fetch_off_payload(url: str, token: str) -> tuple[int, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers={"Authorization": "Bearer %s" % token})
        if response.status_code != 200:
            return response.status_code, None
        try:
            return 200, response.json()
        except ValueError:
            return 200, None


async def refresh_once(*, fetch: Any = None, token_provider: Any = None, path: Path | None = None) -> bool:
    """Ask the cloud once. Returns True when a good answer was applied.

    Fails open: no token, an unreachable cloud, any status but 200, or an
    unreadable body leaves the current list (the cached one, or empty) alone.
    """
    token = await (token_provider or _desktop_access_token)()
    if not token:
        return False
    url = _cloud_base_url() + ENDPOINT_PATH
    try:
        status, payload = await (fetch or _fetch_off_payload)(url, token)
    except Exception as exc:  # noqa: BLE001, RUF100 - fail open on any transport error
        logger.info("Coming-soon refresh: cloud not reachable (%s); keeping the current list", type(exc).__name__)
        return False
    if status != 200:
        logger.info("Coming-soon refresh: cloud answered %s; keeping the current list", status)
        return False
    keys = parse_off_payload(payload)
    if keys is None:
        logger.warning("Coming-soon refresh: unreadable answer; keeping the current list")
        return False
    apply_off(keys)
    save_cache(keys, path)
    return True


def load_cached_state(path: Path | None = None) -> frozenset[str]:
    """Apply the cached list at launch (empty when there is no cache)."""
    cached = load_cache(path)
    if cached is not None:
        return apply_off(cached)
    return _off


async def run_desktop_refresh_loop(*, interval_seconds: float = REFRESH_SECONDS) -> None:
    """Launch, sign-in and every-15-minutes refresh. Runs until cancelled."""
    load_cached_state()
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_account_change() -> None:
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass

    listener_installed = False
    try:
        from auth.desktop_session import add_account_change_listener

        add_account_change_listener(_on_account_change)
        listener_installed = True
    except Exception:  # noqa: BLE001, RUF100 - the timer still refreshes
        logger.debug("Coming-soon refresh: no account-change signal", exc_info=True)
    try:
        while True:
            try:
                await refresh_once()
            except Exception:  # noqa: BLE001, RUF100 - never let the loop die
                logger.debug("Coming-soon refresh failed", exc_info=True)
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
    finally:
        if listener_installed:
            try:
                from auth.desktop_session import remove_account_change_listener

                remove_account_change_listener(_on_account_change)
            except Exception:  # noqa: BLE001, RUF100
                pass


def reset_for_tests() -> None:
    apply_off(())
