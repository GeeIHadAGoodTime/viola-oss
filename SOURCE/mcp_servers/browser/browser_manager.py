"""Browser lifecycle management for the Playwright MCP server.

The browser process is shared, but each top-level Viola task runs in its own
fresh Playwright context.  The context starts from a filtered identity snapshot
and merges back only auth-looking state at task end so stale carts and checkout
cookies do not pollute later commands.

Multi-user: task contexts and persistent identity snapshots are scoped by
user_id.  Parallel tasks from the same user get distinct task contexts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import tempfile
import time as _time_mod
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from filelock import FileLock, Timeout as FileLockTimeout

from core.platform import get_data_dir
from mcp_servers.browser.safety_helpers import _ALLOWED_SCHEMES, _PRIVATE_NETWORKS
from services.browser.task_session import (
    BrowserTaskSession,
    empty_storage_state,
    filter_ephemeral_storage_state,
    merge_auth_storage_state,
    normalize_storage_state,
    storage_state_has_entries,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, Route

_NAV_TIMEOUT_MS = 30_000
_ACTION_TIMEOUT_MS = 10_000
_MAX_TEXT_LENGTH = 5000

# Idle browser context timeout (seconds)
_CONTEXT_IDLE_TIMEOUT = 1800  # 30 minutes
# Hard cap on concurrent per-user browser contexts
_MAX_USER_CONTEXTS = 50
_CHROMIUM_JS_HEAP_CAP_MB = 512
_CHROMIUM_OOM_GUARD_ARGS = ("--js-flags=--max-old-space-size=512",)
_CHROMIUM_RENDERING_ARGS = (
    "--enable-gpu",
    "--use-gl=swiftshader",
    "--use-angle=swiftshader",
    "--force-color-profile=srgb",
    "--blink-settings=imagesEnabled=true",
)
_IDENTITY_STORAGE_STATE_FILE = "viola_identity_storage_state.json"
_IDENTITY_MERGE_LOCK_FILE = ".merge.lock"
_MANUAL_BROWSER_TASK_ID = "manual"


def _chromium_resource_args() -> list[str]:
    """Return Chromium process args that bound renderer memory growth."""
    return [*_CHROMIUM_OOM_GUARD_ARGS, *_CHROMIUM_RENDERING_ARGS]


def _cloud_surface_default_headless() -> bool:
    """Return True for cloud/container surfaces that cannot show a browser."""
    app_surface = os.environ.get("VIOLA_APP_SURFACE", "").strip().lower()
    return app_surface == "cloud" or os.environ.get("VIOLA_CLOUD_BACKEND", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_phone_browser_task_id(task_id: str | None) -> bool:
    return str(task_id or "").strip().lower().startswith("phone:")


async def _auto_handle_dialog(dialog: Any) -> None:
    """Auto-handle JS dialogs so they never block page interaction.

    Alerts are accepted (OK). Confirm and prompt dialogs are dismissed (Cancel).
    """
    try:
        if dialog.type == "alert":
            await dialog.accept()
        else:
            await dialog.dismiss()
    except Exception:
        logger.debug("Dialog auto-handle failed, dialog may have already closed")


class BrowserManager:
    """Manages Playwright browser lifecycle for the MCP server.

    The browser launches on first use (lazy) and persists across tool calls.
    A single browser context maintains cookies, storage, and login state.
    Pages are reused when possible (navigate existing page vs open new).
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        # S7-05 multi-tenant: API logs are keyed by user_id so a tenant cannot
        # read another tenant's captured URLs, request bodies, or response
        # previews via ``browser_get_api_log``.  The previous code stored
        # one global list shared across every connected user.
        self._api_log_by_user: dict[str, list[dict]] = {}
        self._API_LOG_MAX = 100  # Memory bounded per-user
        self._ref_map: dict[str, dict[str, Any]] = {}  # ref_id -> {role, name}
        self._ephemeral_dir: str | None = None  # Temp dir for ephemeral mode
        self._launched_mode: str | None = None  # Mode used for current browser instance
        self._active_user_id: str | None = None
        self._user_id_resolver: Callable[[], str | None] | None = None
        self._task_id_resolver: Callable[[], str | None] | None = None
        self._active_pool_key: tuple[str, str] | None = None
        self._pool_ref_maps: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        # Per-task browser context isolation for multi-user SaaS mode
        self._task_sessions: dict[tuple[str, str], BrowserTaskSession] = {}
        self._task_ref_maps: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._merge_locks: dict[str, asyncio.Lock] = {}
        self._task_context_opts: dict[str, Any] = {}
        self._persistent_context_opts: dict[str, Any] = {}
        self._browser_launch_opts: dict[str, Any] = {}
        # Legacy per-user maps are retained for compatibility with older tests
        # and diagnostic code; active browser pages now live in _task_sessions.
        self._user_contexts: dict[str, Any] = {}  # user_id -> BrowserContext
        self._user_pages: dict[str, Any] = {}  # user_id -> Page
        self._context_last_used: dict[str, float] = {}  # user_id -> timestamp

    def set_user_id_resolver(self, resolver: Callable[[], str | None]) -> None:
        """Register a callback that resolves the current request user_id."""
        self._user_id_resolver = resolver

    def set_task_id_resolver(self, resolver: Callable[[], str | None]) -> None:
        """Register a callback that resolves the current top-level browser task."""
        self._task_id_resolver = resolver

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        resolved = user_id
        if not resolved and self._user_id_resolver is not None:
            try:
                resolved = self._user_id_resolver()
            except Exception:
                logger.debug("Browser user_id resolver failed")
        if not resolved or not resolved.strip():
            try:
                from core.user_context import get_current_user_id

                resolved = get_current_user_id()
            except Exception:
                logger.debug("Browser current-user resolution failed")
        if not resolved or not resolved.strip():
            raise ValueError("BrowserManager requires user_id")
        from core.user_context import is_placeholder_user_id

        if is_placeholder_user_id(resolved):
            raise ValueError("BrowserManager refuses sentinel user_id")
        return resolved.strip()

    def _resolve_task_id_or_none(self, task_id: str | None = None) -> str | None:
        resolved = task_id
        if not resolved and self._task_id_resolver is not None:
            try:
                resolved = self._task_id_resolver()
            except Exception:
                logger.debug("Browser task_id resolver failed")
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
        return None

    def _resolve_task_id(self, task_id: str | None = None) -> str:
        """Resolve the browser task id from MCP metadata or a manual fallback."""
        return self._resolve_task_id_or_none(task_id) or _MANUAL_BROWSER_TASK_ID

    @staticmethod
    def _is_truthy(value: str | None) -> bool:
        return (value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _use_cloud_session_pool(self) -> bool:
        if _is_phone_browser_task_id(self._resolve_task_id_or_none()):
            return True
        if self._is_truthy(os.environ.get("VIOLA_DISABLE_CLOUD_BROWSER_POOL")):
            return False
        if _cloud_surface_default_headless():
            return True
        try:
            from config.settings import settings
            from services.computer_use.cloud_guard import is_cloud_surface

            return bool(is_cloud_surface(settings))
        except (ImportError, AttributeError, TypeError, ValueError):
            return _cloud_surface_default_headless()

    def _current_pool_ref_key(self) -> tuple[str, str] | None:
        if not self._use_cloud_session_pool():
            return None
        user_id = self._resolve_user_id()
        task_id = self._resolve_task_id_or_none()
        if task_id:
            return user_id, task_id
        if self._active_pool_key is not None and self._active_pool_key[0] == user_id:
            return self._active_pool_key
        return None

    async def _acquire_cloud_session(self, user_id: str, task_id: str | None = None) -> Any:
        from services.browser.cloud_session_pool import get_cloud_browser_session_pool

        pool = get_cloud_browser_session_pool()
        resolved_task_id = self._resolve_task_id_or_none(task_id)
        if not resolved_task_id:
            existing = await pool.lookup_for_user(user_id)
            resolved_task_id = existing.task_id if existing is not None else "mcp:%s" % user_id
        session = await pool.acquire(
            user_id=user_id,
            task_id=resolved_task_id,
            context_options={
                "viewport": {"width": 1280, "height": 800},
                "ignore_https_errors": False,
            },
            launch_args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-default-browser-check",
                "--no-first-run",
                *_chromium_resource_args(),
            ],
            metadata={"consumer": "mcp-browser"},
        )
        self._bind_cloud_session(session)
        await pool.release(user_id=session.user_id, task_id=session.task_id)
        return session

    def _bind_cloud_session(self, session: Any) -> None:
        self._browser = session.browser
        self._context = session.context
        self._page = session.page
        self._active_user_id = session.user_id
        self._active_pool_key = (session.user_id, session.task_id)
        self._launched_mode = "cloud-pool"
        if not session.metadata.get("mcp_events_attached"):
            try:
                session.page.on("dialog", lambda d: asyncio.ensure_future(_auto_handle_dialog(d)))
                session.page.on(
                    "response",
                    lambda resp: asyncio.ensure_future(self._on_response(resp, user_id=session.user_id)),
                )
                session.metadata["mcp_events_attached"] = True
            except (AttributeError, RuntimeError, TypeError, ValueError):
                logger.debug("Failed to attach MCP page event handlers to pooled page")

    def _task_key(self, user_id: str | None = None, task_id: str | None = None) -> tuple[str, str]:
        return (self._resolve_user_id(user_id), self._resolve_task_id(task_id))

    def _current_task_session(self) -> BrowserTaskSession | None:
        return self._task_sessions.get(self._task_key())

    def _current_page(self) -> Any | None:
        session = self._current_task_session()
        if session is not None:
            return session.page
        return self._page

    def _current_context(self) -> Any | None:
        session = self._current_task_session()
        if session is not None:
            return session.context
        return self._context

    def _current_ref_map(self) -> dict[str, dict[str, Any]]:
        pool_key = self._current_pool_ref_key()
        if pool_key is not None:
            return self._pool_ref_maps.setdefault(pool_key, {})
        key = self._task_key()
        return self._task_ref_maps.setdefault(key, {})

    @staticmethod
    def _safe_user_segment(user_id: str) -> str:
        """Return a filesystem-safe directory name for a user."""
        return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in user_id)

    @staticmethod
    def _browser_profile_base_dir() -> Path:
        """Return the Viola-owned browser profile root."""

        configured = os.environ.get("VIOLA_BROWSER_PROFILE_ROOT", "").strip()
        if configured:
            return Path(configured).expanduser()
        return get_data_dir() / "browser_profiles"

    def _profile_root_for_user(self, user_id: str) -> Path:
        """Return the per-user browser profile root."""
        return self._browser_profile_base_dir() / self._safe_user_segment(user_id)

    def _identity_profile_for_mode(self, user_id: str, mode: str) -> Path | None:
        """Return the persistent identity profile for *mode*, or None for solo."""
        profile_root = self._profile_root_for_user(user_id)
        if mode == "ephemeral":
            return None
        if mode == "own":
            return profile_root / "own_chrome_profile"
        return profile_root

    @staticmethod
    def _identity_storage_state_path(identity_profile: Path) -> Path:
        return identity_profile / _IDENTITY_STORAGE_STATE_FILE

    @staticmethod
    def _identity_merge_lock_path(identity_profile: Path) -> Path:
        return identity_profile / _IDENTITY_MERGE_LOCK_FILE

    def _merge_lock_for_user(self, user_id: str) -> asyncio.Lock:
        lock = self._merge_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._merge_locks[user_id] = lock
        return lock

    async def _get_user_context(self, user_id: str) -> Any:
        """Return or create an isolated BrowserContext for *user_id*.

        Each user gets a separate persistent context with its own
        cookies, storage, and login state.  Contexts idle for more than
        ``_CONTEXT_IDLE_TIMEOUT`` seconds are closed on the next access
        by *any* user (lazy cleanup).
        """
        # Lazy cleanup of idle contexts
        await self._cleanup_idle_contexts()

        self._context_last_used[user_id] = _time_mod.monotonic()

        existing = self._user_contexts.get(user_id)
        if existing is not None:
            return existing

        # Create a new persistent context for this user
        if self._playwright is None:
            return self._context  # Fall back to primary context

        profile = self._profile_root_for_user(user_id)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            ctx = await self._playwright.chromium.launch_persistent_context(
                str(profile),
                headless=True,
                viewport={"width": 1280, "height": 800},
            )
            await self._install_network_guard(ctx)
            self._user_contexts[user_id] = ctx
            logger.info("Created isolated browser context for user %s", user_id)
            return ctx
        except Exception as exc:
            logger.warning(
                "Failed to create per-user context for %s, sharing primary: %s",
                user_id,
                exc,
            )
            return self._context

    async def _cleanup_idle_contexts(self) -> None:
        """Close browser contexts idle for more than *_CONTEXT_IDLE_TIMEOUT*.

        Also enforces _MAX_USER_CONTEXTS by evicting the least-recently-used
        contexts when the cap is exceeded.
        """
        now = _time_mod.monotonic()
        stale_users: list[str] = []
        for uid, last_used in self._context_last_used.items():
            if now - last_used > _CONTEXT_IDLE_TIMEOUT:
                stale_users.append(uid)

        for uid in stale_users:
            await self._close_user_context(uid)

        # Hard cap: evict oldest contexts if over limit
        if len(self._user_contexts) >= _MAX_USER_CONTEXTS:
            sorted_by_age = sorted(self._context_last_used.items(), key=lambda x: x[1])
            excess = len(self._user_contexts) - _MAX_USER_CONTEXTS + 1
            for uid, _ts in sorted_by_age[:excess]:
                await self._close_user_context(uid)

    async def _close_user_context(self, uid: str) -> None:
        """Close and remove a single user's browser context."""
        ctx = self._user_contexts.pop(uid, None)
        page = self._user_pages.pop(uid, None)
        self._context_last_used.pop(uid, None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                logger.debug("Failed to close idle page for user %s", uid)
        if ctx is not None:
            try:
                await ctx.close()
                logger.info("Closed idle browser context for user %s", uid)
            except Exception:
                logger.debug("Failed to close idle context for user %s", uid)

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently launched."""
        if self._browser is not None and getattr(self._browser, "is_connected", lambda: True)():
            return True
        # Persistent contexts don't use a separate Browser object
        return self._context is not None

    def is_running_for_user(self, user_id: str | None = None) -> bool:
        """Whether the active browser belongs to the resolved user."""
        resolved = self._resolve_user_id(user_id)
        if self._use_cloud_session_pool():
            from services.browser.cloud_session_pool import get_cloud_browser_session_pool

            return get_cloud_browser_session_pool().has_sessions_for_user(resolved)
        if not self.is_running:
            return False
        return self._active_user_id in (None, resolved) or any(key[0] == resolved for key in self._task_sessions)

    async def ensure_browser(self, user_id: str | None = None) -> None:
        """Launch browser if not running, or relaunch if mode changed.

        Reads browser_session_mode from disk on every call to detect tier
        switches (Solo/Ensemble/Symphony).  If the mode changed since the
        last launch, shuts down the current browser and relaunches in the
        new mode.  This prevents state leaks between tiers while keeping
        per-task contexts isolated inside the process.
        """
        resolved_user_id = self._resolve_user_id(user_id)
        if self._use_cloud_session_pool():
            await self._acquire_cloud_session(resolved_user_id)
            return
        async with self._lock:
            # Detect mode changes by reading settings.json from disk
            # (the SettingsManager singleton may be stale in subprocesses).
            current_mode = self._read_mode_from_disk()
            if self.is_running and self._launched_mode and current_mode != self._launched_mode:
                logger.info(
                    "Browser mode changed %s -> %s, restarting browser",
                    self._launched_mode,
                    current_mode,
                )
                await self._close_internal()
            if self.is_running:
                self._active_user_id = resolved_user_id
                return
            await self._launch(resolved_user_id)

    async def get_page(self, user_id: str | None = None, task_id: str | None = None) -> Page:
        """Get the active page. Creates one if none exists.

        If the browser context has crashed (Chromium process died), auto-relaunches
        before creating a new page. This prevents the "Target page, context or
        browser has been closed" error from propagating to tool callers.
        """
        resolved_user_id = self._resolve_user_id(user_id)
        if self._use_cloud_session_pool():
            session = await self._acquire_cloud_session(resolved_user_id, task_id)
            return session.page
        await self.ensure_browser(resolved_user_id)
        async with self._lock:
            resolved_task_id = self._resolve_task_id(task_id)
            session_key = (resolved_user_id, resolved_task_id)
            session = self._task_sessions.get(session_key)
            if session is None and not self._task_sessions and self._page is not None and not self._page.is_closed():
                return self._page
            if session is None:
                session = await self._create_task_session(resolved_user_id, resolved_task_id)
            self._active_user_id = resolved_user_id
            self._context = session.context
            if session.page is not None and not session.page.is_closed():
                self._page = session.page
                return session.page
            try:
                session.page = await session.context.new_page()
            except Exception:
                # Context is dead (Chromium crashed or was killed). Discard
                # this task's ephemeral state and recreate it from identity.
                logger.warning("Browser task context is dead, resetting task context")
                await self._discard_task_session(session_key)
                await self._close_internal()
                await self._launch(resolved_user_id)
                session = await self._create_task_session(resolved_user_id, resolved_task_id)
                session.page = await session.context.new_page()
            session.page.on("dialog", lambda d: asyncio.ensure_future(_auto_handle_dialog(d)))
            # S7-05 multi-tenant: bind the owner so captured responses land in
            # their bucket, not a shared global list.
            _bound_user = resolved_user_id
            session.page.on(
                "response",
                lambda resp: asyncio.ensure_future(self._on_response(resp, user_id=_bound_user)),
            )
            self._context = session.context
            self._page = session.page
            return session.page

    # -- element ref map ---------------------------------------------------

    def clear_ref_map(self) -> None:
        """Clear ref map. Called before every new snapshot."""
        ref_map = self._current_ref_map()
        ref_map.clear()
        self._ref_map = ref_map

    def restrict_ref_map(self, visible_refs: set[str]) -> None:
        """Remove refs not visible in the snapshot text.

        Called after footer/combobox collapse so the agent cannot click
        elements it cannot see.  This prevents stale-ref misrouting —
        e.g. @e58 from a closed dialog resolving to a footer allergen link
        on the new page.
        """
        ref_map = self._current_ref_map()
        stale = [k for k in ref_map if k not in visible_refs]
        for k in stale:
            del ref_map[k]
        self._ref_map = ref_map

    def set_ref(
        self,
        ref_id: str,
        role: str,
        name: str,
        frame: Any = None,
        role_index: int = 0,
        name_index: int = 0,
    ) -> None:
        """Register a ref from the latest snapshot.

        Args:
            ref_id: The ref identifier (e.g. "e1").
            role: ARIA role (e.g. "button", "textbox").
            name: Accessible name of the element.
            frame: Playwright Frame that owns this element (None = main page).
            role_index: Absolute index among all elements with the same role
                on the page.  Used as fallback for unnamed elements.
            name_index: Index among elements with the same (role, name) pair.
                Used to disambiguate when responsive nav duplicates links.
        """
        ref_map = self._current_ref_map()
        ref_map[ref_id] = {
            "role": role,
            "name": name,
            "frame": frame,
            "role_index": role_index,
            "name_index": name_index,
        }
        self._ref_map = ref_map

    def get_ref_metadata(self, ref_id: str) -> dict[str, Any] | None:
        """Return stored metadata for a ref from the latest snapshot."""
        if ref_id.startswith("@"):
            ref_id = ref_id[1:]
        entry = self._current_ref_map().get(ref_id)
        return dict(entry) if entry else None

    def resolve_ref(self, ref_id: str) -> Locator:
        """Resolve @eN ref to a Playwright locator using ARIA role+name.

        Returns a lazy Locator — the caller awaits the action (click/fill/etc)
        which triggers the actual DOM lookup.

        Accepts both "e1" and "@e1" forms (LLMs often include the @ prefix).

        Raises ValueError if ref not found in map.
        """
        resolved_user_id = self._resolve_user_id()
        if self._active_user_id and self._active_user_id != resolved_user_id:
            raise ValueError("Browser session changed users. Take a fresh snapshot before reusing refs.")

        # Strip leading @ — LLMs send "@e1" but ref_map keys are "e1".
        if ref_id.startswith("@"):
            ref_id = ref_id[1:]
        ref_map = self._current_ref_map()
        entry = ref_map.get(ref_id)
        if not entry:
            # Fallback: LLMs sometimes send CSS selectors (#id, .class, [attr])
            # instead of @eN refs.  If it looks like a CSS selector, use it
            # directly — Playwright handles them natively.
            if ref_id.startswith(("#", ".", "[")) or ">" in ref_id:
                page = self._current_page()
                if not page or page.is_closed():
                    raise ValueError("No active page. Navigate to a URL first.")
                return page.locator(ref_id)
            valid = sorted(
                ref_map.keys(),
                key=lambda x: int(x[1:]) if x[1:].isdigit() else 0,
            )[:20]
            valid_str = ", ".join("@%s" % r for r in valid) if valid else "(none)"
            raise ValueError(
                "Ref @%s not found. The page may have changed. "
                "Use browser_snapshot to get fresh refs. "
                "Valid refs: %s" % (ref_id, valid_str)
            )
        role = entry["role"]
        name = entry["name"]
        frame = entry.get("frame")
        role_index = entry.get("role_index", 0)
        name_index = entry.get("name_index", 0)
        page = self._current_page()
        if not page or page.is_closed():
            raise ValueError("No active page. Navigate to a URL first.")
        # Use the frame that owns this ref (iframe elements) or fall back
        # to the main page (top-level elements).
        target = frame if frame is not None else page
        if name:
            locator = target.get_by_role(role, name=name, exact=True)
            # Disambiguate when multiple elements share the same role+name
            # (common on responsive sites with mobile + desktop nav).
            if name_index > 0:
                locator = locator.nth(name_index)
            return locator
        # Unnamed elements: resolve by role + absolute index on the page.
        return target.get_by_role(role).nth(role_index)

    def detect_navigation(self, previous_url: str) -> bool:
        """Check if the page URL changed (same-tab navigation).

        Args:
            previous_url: URL before an action was taken.

        Returns:
            True if the page navigated to a different URL.
        """
        page = self._current_page()
        if not page or page.is_closed():
            return False
        current = page.url
        # Normalize: strip trailing slashes, ignore fragment-only changes
        prev_clean = previous_url.rstrip("/").split("#")[0]
        curr_clean = current.rstrip("/").split("#")[0]
        return prev_clean != curr_clean

    async def navigate(self, url: str, user_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        """Navigate to URL. Returns page title, URL, description, and form field count."""
        page = await self.get_page(user_id, task_id)
        response = await page.goto(
            url,
            timeout=_NAV_TIMEOUT_MS,
            wait_until="domcontentloaded",
        )
        # Wait briefly for SPA frameworks to render after initial load.
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            logger.debug("networkidle wait timed out after navigation, continuing")
        # Auto-dismiss cookie consent dialogs (common on virtually all sites).
        await self._dismiss_cookie_consent(page)
        title = await page.title()
        text = ""
        try:
            text = await page.inner_text("body", timeout=_ACTION_TIMEOUT_MS)
        except Exception:
            logger.debug("Could not read page body text after navigation")
        if len(text) > 500:
            text = text[:500] + "..."
        result: dict[str, Any] = {"title": title, "url": page.url, "description": text}
        if response is not None:
            result["status"] = getattr(response, "status", None)
            result["http_status_code"] = getattr(response, "status", None)
            redirect_chain: list[str] = []
            try:
                request = response.request
                chain: list[str] = [str(getattr(request, "url", "") or "")]
                prior = getattr(request, "redirected_from", None)
                if callable(prior):
                    prior = prior()
                while prior is not None:
                    chain.append(str(getattr(prior, "url", "") or ""))
                    prior = getattr(prior, "redirected_from", None)
                    if callable(prior):
                        prior = prior()
                redirect_chain = [item for item in reversed(chain) if item]
                final_url = str(getattr(response, "url", "") or page.url)
                if final_url and (not redirect_chain or redirect_chain[-1] != final_url):
                    redirect_chain.append(final_url)
            except (AttributeError, RuntimeError, TypeError):
                logger.debug("Could not derive redirect chain after navigation")
            result["redirect_chain"] = redirect_chain
            result["redirect_chain_known"] = True
        # Include form field count so the agent immediately knows if there's a form.
        try:
            field_count = await page.evaluate(
                "() => document.querySelectorAll(" "'input:not([type=hidden]), textarea, select').length"
            )
            if field_count > 0:
                result["form_fields"] = field_count
        except Exception:
            logger.debug("Could not count form fields after navigation")
        return result

    @staticmethod
    async def _dismiss_cookie_consent(page: Any) -> None:
        """Try to dismiss common cookie consent dialogs.

        Uses JavaScript to find and click consent buttons across common
        cookie consent libraries (OneTrust, CookieBot, TrustArc, GDPR
        banners, etc.). This is a best-effort operation — failure is silent.
        """
        try:
            await page.evaluate("""() => {
                    // Common cookie consent button selectors across libraries.
                    const selectors = [
                        // OneTrust (Dominos, many Fortune 500)
                        '#onetrust-accept-btn-handler',
                        '#accept-recommended-btn-handler',
                        // CookieBot
                        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
                        '#CybotCookiebotDialogBodyButtonAccept',
                        // TrustArc / TrustE
                        '.truste-consent-button[data-behavior="accept"]',
                        // Generic patterns
                        'button[data-testid*="cookie-accept"]',
                        'button[data-testid*="consent-accept"]',
                        'button[id*="cookie"][id*="accept"]',
                        'button[id*="consent"][id*="accept"]',
                        'button[class*="cookie"][class*="accept"]',
                        'button[class*="consent"][class*="accept"]',
                        // GDPR consent banners
                        '.cookie-consent-accept',
                        '.gdpr-accept',
                        '.cc-accept',
                        '.js-cookie-accept',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn) { btn.click(); return; }
                    }
                    // Fallback: look for buttons with accept/allow text in
                    // cookie consent containers.
                    const containers = document.querySelectorAll(
                        '[role=dialog], [class*=consent], [class*=cookie-banner], '
                        + '[id*=consent], [id*=cookie-banner], [class*=privacy]'
                    );
                    for (const c of containers) {
                        const btns = c.querySelectorAll('button, a[role=button], [role=button]');
                        for (const b of btns) {
                            const t = (b.innerText || '').toLowerCase().trim();
                            if (/accept|allow|agree|got it|ok|continue/i.test(t)
                                && !/reject|decline|manage|settings|customize/i.test(t)) {
                                b.click();
                                return;
                            }
                        }
                    }
                }""")
            # Brief wait for the consent dialog to close.
            await page.wait_for_timeout(500)
        except Exception:
            logger.debug("Cookie consent dismissal failed, continuing")

    # -- popup / new-tab following ------------------------------------------

    def begin_popup_watch(self) -> tuple[asyncio.Future, Any, str]:
        """Set up a listener for new tab/popup events before a click.

        Call this *before* performing a click that might open a new tab
        (``target="_blank"``, ``window.open()``, etc.).

        Returns:
            ``(future, cleanup_fn, previous_url)`` — pass all three to
            :meth:`resolve_popup_watch` after the click completes.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        previous_url = ""
        page = self._current_page()
        context = self._current_context()
        if page and not page.is_closed():
            previous_url = page.url

        def on_new_page(page: Any) -> None:
            if not future.done():
                future.set_result(page)

        if context:
            context.on("page", on_new_page)

        def cleanup() -> None:
            if context:
                try:
                    context.remove_listener("page", on_new_page)
                except Exception:
                    logger.debug("Failed to remove popup listener, may already be removed")

        return future, cleanup, previous_url

    async def resolve_popup_watch(
        self,
        future: asyncio.Future,
        cleanup: Any,
        previous_url: str,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        """Check for and follow a new tab that opened during a click.

        Always cleans up the popup listener.  Safe to call even when no
        popup was opened (returns ``{"new_tab": False}``).
        """
        try:
            try:
                new_page = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=timeout,
                )
            except TimeoutError:
                return {"new_tab": False}

            if new_page.is_closed():
                return {"new_tab": False}

            # Wait for the new page to load.
            try:
                await new_page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception:
                logger.debug("domcontentloaded wait timed out for new tab")
            try:
                await new_page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                logger.debug("networkidle wait timed out for new tab")

            if new_page.is_closed():
                return {"new_tab": False}

            # Switch the active page to the new tab.
            session = self._current_task_session()
            if session is not None:
                session.page = new_page
            self._page = new_page
            new_page.on(
                "dialog",
                lambda d: asyncio.ensure_future(_auto_handle_dialog(d)),
            )
            # Bind the popup's response capture to the same owner as the
            # originating page so multi-tenant API logs stay partitioned.
            _bound_user = self._active_user_id or self._resolve_user_id(None)
            new_page.on(
                "response",
                lambda resp: asyncio.ensure_future(self._on_response(resp, user_id=_bound_user)),
            )
            self.clear_ref_map()

            return {
                "new_tab": True,
                "previous_url": previous_url,
                "new_url": new_page.url,
            }
        finally:
            cleanup()

    async def shutdown(self, user_id: str | None = None) -> None:
        """Close browser and cleanup Playwright."""
        if self._use_cloud_session_pool():
            from services.browser.cloud_session_pool import get_cloud_browser_session_pool

            resolved_user_id = self._resolve_user_id(user_id)
            pool = get_cloud_browser_session_pool()
            task_id = self._resolve_task_id_or_none()
            if task_id:
                await pool.close_session(user_id=resolved_user_id, task_id=task_id)
                self._pool_ref_maps.pop((resolved_user_id, task_id), None)
            else:
                await pool.close_user_sessions(resolved_user_id)
                for key in [key for key in self._pool_ref_maps if key[0] == resolved_user_id]:
                    self._pool_ref_maps.pop(key, None)
            if self._active_user_id == resolved_user_id:
                self._browser = None
                self._context = None
                self._page = None
                self._active_pool_key = None
                self._active_user_id = None
            return
        async with self._lock:
            await self._close_internal()

    # -- network interception ----------------------------------------------

    async def _on_response(self, response: Any, *, user_id: str | None = None) -> None:
        """Capture JSON API responses for later analysis (Phase 3 learning).

        Multi-tenant: entries are partitioned by ``user_id`` (the owner of
        the page that triggered this response).  If the owner cannot be
        determined we DROP the entry — it is safer to lose a debug log
        than to merge it into a shared bucket where another tenant could
        read it.
        """
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            if response.status < 200 or response.status >= 400:
                return

            url = str(response.url)

            # Skip static assets, analytics, tracking
            _SKIP_PATTERNS = (
                "google-analytics",
                "googletagmanager",
                "facebook.com",
                "doubleclick",
                "hotjar",
                "segment.io",
                "sentry.io",
                "newrelic",
                "mixpanel",
                "amplitude",
                "intercom",
                ".js",
                ".css",
                ".png",
                ".jpg",
                ".svg",
                ".woff",
                "favicon",
                "manifest.json",
            )
            if any(p in url.lower() for p in _SKIP_PATTERNS):
                return

            # Skip auth-related endpoints (privacy)
            _AUTH_PATTERNS = (
                "/login",
                "/auth",
                "/token",
                "/password",
                "/oauth",
                "/signin",
                "/signup",
                "/session",
                "/credentials",
                "/2fa",
                "/mfa",
            )
            if any(p in url.lower() for p in _AUTH_PATTERNS):
                return

            request = response.request

            # Try to get response body (non-blocking, may fail)
            try:
                body = await response.json()
            except Exception:
                return

            body_str = str(body)
            if len(body_str) > 10000:
                body = "<truncated: %d chars>" % len(body_str)

            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "url": url,
                "method": request.method,
                "request_headers": dict(request.headers) if request.headers else {},
                "request_body": request.post_data,
                "status": response.status,
                "response_body": body,
            }

            # Multi-tenant: refuse to record without an owner — the only
            # other option is to merge entries into a shared global list,
            # which is exactly the leak this guard prevents.
            owner = user_id or self._active_user_id
            if not owner:
                logger.debug("API response capture: dropping entry — no resolved owner user_id")
                return
            bucket = self._api_log_by_user.setdefault(owner, [])
            bucket.append(entry)
            if len(bucket) > self._API_LOG_MAX:
                self._api_log_by_user[owner] = bucket[-self._API_LOG_MAX :]

        except Exception:
            logger.debug("API response capture failed, skipping entry")

    def get_api_log(
        self,
        domain_filter: str | None = None,
        *,
        user_id: str | None = None,
    ) -> list[dict]:
        """Get captured API calls for ``user_id``, optionally filtered by domain.

        Multi-tenant: ``user_id`` is REQUIRED.  Without it, a tenant
        calling ``browser_get_api_log`` would receive entries captured
        for other tenants who happened to share the process browser.
        """
        if not user_id:
            logger.warning("get_api_log refused: user_id is required")
            return []
        bucket = self._api_log_by_user.get(user_id) or []
        if not domain_filter:
            return list(bucket)
        return [e for e in bucket if domain_filter.lower() in e["url"].lower()]

    def clear_api_log(self, *, user_id: str | None = None) -> None:
        """Clear the API capture log.

        With ``user_id`` only that tenant's bucket is cleared; without it
        every tenant's bucket is cleared (used on shutdown / restart).
        """
        if user_id is None:
            self._api_log_by_user.clear()
            return
        self._api_log_by_user.pop(user_id, None)

    # -- per-task snapshot/discard lifecycle -------------------------------

    async def _create_task_session(self, user_id: str, task_id: str) -> BrowserTaskSession:
        """Create a fresh task context from the user's filtered identity snapshot."""
        assert self._browser is not None
        mode = self._launched_mode or self._read_mode_from_disk()
        identity_profile = self._identity_profile_for_mode(user_id, mode)
        loaded_snapshot = empty_storage_state()
        if identity_profile is not None:
            try:
                loaded_snapshot = await self._load_identity_snapshot(identity_profile)
            except Exception as exc:
                logger.warning(
                    "Browser identity snapshot read failed for user %s; starting anonymous task: %s",
                    user_id,
                    exc,
                )
                loaded_snapshot = empty_storage_state()

        context_opts = dict(self._task_context_opts)
        if storage_state_has_entries(loaded_snapshot):
            context_opts["storage_state"] = loaded_snapshot
        try:
            context = await self._browser.new_context(**context_opts)
        except Exception as exc:
            logger.warning(
                "Browser storage_state load failed for user %s task %s; starting anonymous task: %s",
                user_id,
                task_id,
                exc,
            )
            context_opts.pop("storage_state", None)
            context = await self._browser.new_context(**context_opts)

        await self._apply_context_hardening(context)
        await self._install_network_guard(context)
        session = BrowserTaskSession(
            user_id=user_id,
            task_id=task_id,
            mode=mode,
            context=context,
            identity_profile=identity_profile,
            loaded_snapshot=loaded_snapshot,
        )
        self._task_sessions[session.key] = session
        self._context_last_used[user_id] = _time_mod.monotonic()
        logger.info(
            "Created browser task context user=%s task=%s mode=%s identity=%s",
            user_id,
            task_id,
            mode,
            "none" if identity_profile is None else str(identity_profile),
        )
        return session

    async def end_task(
        self,
        user_id: str | None = None,
        task_id: str | None = None,
        *,
        merge_auth: bool = True,
    ) -> bool:
        """Merge auth state for one task and close its ephemeral context."""
        key = self._task_key(user_id, task_id)
        async with self._lock:
            session = self._task_sessions.pop(key, None)
            self._task_ref_maps.pop(key, None)
            if session is not None and self._page is session.page:
                self._page = None
            if session is not None and self._context is session.context:
                self._context = None
        if session is None:
            return False
        await self._finalize_task_session(session, merge_auth=merge_auth)
        return True

    async def _discard_task_session(self, key: tuple[str, str]) -> None:
        """Close a task context without merging state back."""
        session = self._task_sessions.pop(key, None)
        self._task_ref_maps.pop(key, None)
        if session is None:
            return
        with suppress(Exception):
            if session.page is not None and not session.page.is_closed():
                await session.page.close()
        with suppress(Exception):
            await session.context.close()
        if self._page is session.page:
            self._page = None
        if self._context is session.context:
            self._context = None

    async def _close_all_task_sessions(self, *, merge_auth: bool) -> None:
        sessions = list(self._task_sessions.values())
        self._task_sessions.clear()
        self._task_ref_maps.clear()
        self._page = None
        self._context = None
        for session in sessions:
            await self._finalize_task_session(session, merge_auth=merge_auth)

    async def _finalize_task_session(self, session: BrowserTaskSession, *, merge_auth: bool) -> None:
        final_state = empty_storage_state()
        if merge_auth and session.identity_profile is not None:
            try:
                final_state = normalize_storage_state(await session.context.storage_state())
            except Exception as exc:
                logger.warning(
                    "Browser task storage_state capture failed for user %s task %s: %s",
                    session.user_id,
                    session.task_id,
                    exc,
                )
                final_state = empty_storage_state()
        try:
            if session.page is not None and not session.page.is_closed():
                await session.page.close()
        except Exception:
            logger.debug(
                "Browser task page close failed for user %s task %s",
                session.user_id,
                session.task_id,
            )
        try:
            await session.context.close()
        except Exception:
            logger.debug(
                "Browser task context close failed for user %s task %s",
                session.user_id,
                session.task_id,
            )

        if merge_auth and session.identity_profile is not None:
            await self._merge_task_auth_state(session, final_state)

    async def _merge_task_auth_state(self, session: BrowserTaskSession, final_state: dict[str, Any]) -> None:
        identity_profile = session.identity_profile
        if identity_profile is None:
            return
        identity_profile.mkdir(parents=True, exist_ok=True)
        lock_path = self._identity_merge_lock_path(identity_profile)
        async with self._merge_lock_for_user(session.user_id):
            file_lock = FileLock(str(lock_path), timeout=30)
            try:
                file_lock.acquire()
            except FileLockTimeout:
                logger.warning("Timed out waiting for browser identity merge lock: %s", lock_path)
                return
            try:
                try:
                    persistent_state = await self._load_identity_snapshot(identity_profile)
                except Exception as exc:
                    logger.warning(
                        "Browser identity reload failed during merge for user %s; merging into empty state: %s",
                        session.user_id,
                        exc,
                    )
                    persistent_state = empty_storage_state()
                merged = merge_auth_storage_state(
                    original_snapshot=session.loaded_snapshot,
                    final_state=final_state,
                    persistent_state=persistent_state,
                )
                self._write_identity_snapshot_atomic(identity_profile, merged)
            finally:
                with suppress(Exception):
                    file_lock.release()

    async def _load_identity_snapshot(self, identity_profile: Path) -> dict[str, Any]:
        """Load the persistent identity snapshot and filter task-scoped state."""
        state_path = self._identity_storage_state_path(identity_profile)
        if state_path.exists():
            with open(state_path, encoding="utf-8") as handle:
                return filter_ephemeral_storage_state(json.load(handle))

        identity_profile.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            return empty_storage_state()
        context = None
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                str(identity_profile),
                headless=True,
                **self._persistent_context_opts,
            )
            return filter_ephemeral_storage_state(await context.storage_state())
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()

    def _write_identity_snapshot_atomic(self, identity_profile: Path, state: dict[str, Any]) -> None:
        """Persist identity storage state via fsync + atomic replace."""
        identity_profile.mkdir(parents=True, exist_ok=True)
        target = self._identity_storage_state_path(identity_profile)
        fd, tmp_name = tempfile.mkstemp(
            prefix="%s." % target.name,
            suffix=".tmp",
            dir=str(identity_profile),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(normalize_storage_state(state), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                with suppress(OSError):
                    os.unlink(tmp_name)

    @staticmethod
    async def _is_safe_request_url(url: str) -> tuple[bool, str]:
        """Async per-request SSRF check; returns (ok, reason)."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return False, "unparseable URL"
        if parsed.scheme not in _ALLOWED_SCHEMES:
            # Allow data:/blob: URLs (no network egress).
            if parsed.scheme in ("data", "blob", "about", "chrome", "chrome-extension"):
                return True, ""
            return False, "scheme %r not allowed" % parsed.scheme
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return False, "no hostname"
        # Literal IP fast path.
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            for network in _PRIVATE_NETWORKS:
                if literal in network:
                    return False, "literal private IP %s" % literal
            return True, ""
        # DNS path — async to avoid blocking the event loop.
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False, "DNS resolution failed for %s" % host
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            try:
                addr = ipaddress.ip_address(str(sockaddr[0]))
            except ValueError:
                continue
            for network in _PRIVATE_NETWORKS:
                if addr in network:
                    return False, "%s resolves to private/internal %s" % (host, addr)
        return True, ""

    async def _install_network_guard(self, context: Any) -> None:
        """Install per-request SSRF guard mirroring cloud_browser/agent_browser.py.

        Defense-in-depth on top of the pre-navigation _validate_url check in
        safety_helpers: a public URL whose initial validation passed can still
        emit redirects, subresources, iframes, fetch/XHR, or service-worker
        requests targeting private addresses.  This guard re-validates every
        single request the context emits.
        """

        async def _guard(route: Route) -> None:
            url = route.request.url
            ok, reason = await self._is_safe_request_url(url)
            if not ok:
                logger.warning("MCP browser blocked SSRF request: %s (%s)", url[:200], reason)
                with suppress(Exception):
                    await route.abort()
                return
            with suppress(Exception):
                await route.continue_()

        try:
            await context.route("**/*", _guard)
        except Exception as exc:
            logger.error("Failed to install MCP browser network guard: %s", exc)
            raise

    async def _apply_context_hardening(self, context: Any) -> None:
        """Apply stealth/init scripts to a newly created task context."""
        try:
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(context)
            logger.debug("playwright-stealth applied to browser task context")
            return
        except ImportError:
            logger.debug("playwright-stealth not available, using fallback init script")
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true,
            });
            if (!window.chrome) {
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {},
                };
            }
            Object.defineProperty(navigator, 'plugins', {
                get: function() {
                    var arr = [1, 2, 3, 4, 5];
                    arr.item = function(i) { return arr[i]; };
                    arr.namedItem = function(n) { return null; };
                    arr.refresh = function() {};
                    return arr;
                },
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            """)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _read_mode_from_disk() -> str:
        """Read browser_session_mode directly from settings.json on disk.

        Bypasses the SettingsManager singleton which may hold stale values
        in subprocess environments.  Falls back to "viola" if the file
        cannot be read.
        """
        import json

        try:
            from config.settings import settings as _data_settings

            data_dir = Path(getattr(_data_settings, "data_dir", str(get_data_dir())))
        except Exception:
            data_dir = get_data_dir()

        settings_path = data_dir / "settings.json"
        try:
            with open(settings_path, encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("browser_session_mode", "viola"))
        except Exception:
            return "viola"

    async def _launch(self, user_id: str) -> None:
        """Launch Playwright + Chromium."""
        await self._close_internal()

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            msg = "Playwright is not installed. " "Run: pip install playwright && playwright install chromium"
            raise RuntimeError(msg) from exc

        # Resolve headless preference: env var > settings > surface default.
        # Desktop defaults visible; cloud/container surfaces default headless.
        headless: bool
        headless_env = os.environ.get("VIOLA_BROWSER_HEADLESS")
        if headless_env is not None:
            # Explicit env var overrides everything
            headless = headless_env.lower() not in ("false", "0", "no")
        else:
            default_headless = _cloud_surface_default_headless()
            if default_headless:
                headless = True
            else:
                # Fall back to settings on desktop/local surfaces.
                try:
                    from config.settings import settings as _settings

                    headless = bool(getattr(_settings, "browser_headless", False))
                except Exception:
                    headless = False

        # Resolve default geolocation from settings (used to pre-grant
        # permission so native Chromium popups never block the agent).
        default_lat = 43.0147
        default_lng = -87.9956
        try:
            from config.settings import settings as _geo_settings

            default_lat = float(getattr(_geo_settings, "browser_default_latitude", default_lat))
            default_lng = float(getattr(_geo_settings, "browser_default_longitude", default_lng))
        except Exception:
            logger.debug("Could not read geolocation from settings, using defaults")

        # Resolve browser session mode: read directly from disk to detect
        # tier switches (subprocess SettingsManager singleton may be stale).
        mode = self._read_mode_from_disk()

        # Stealth args — remove the primary Playwright bot-detection fingerprints.
        # AutomationControlled is what state/retail sites detect first;
        # --disable-infobars removes the "Chrome is being controlled" banner.
        _stealth_args: list[str] = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage,AutofillEnablePaymentsMandatoryReauth,AutofillSaveCardSignInAfterLocalSave,AutofillEnableVirtualCards",
            "--no-default-browser-check",
            "--no-first-run",
        ]
        _resource_args = _chromium_resource_args()

        # On Windows, browser subprocesses often open behind the parent window.
        # Force the window to a visible position so the user can see it.
        _window_args: list[str] = []
        if not headless:
            _window_args = [
                "--window-position=100,100",
                "--window-size=1280,800",
            ]

        self._task_context_opts = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "permissions": ["geolocation", "notifications", "clipboard-read"],
            "geolocation": {"latitude": default_lat, "longitude": default_lng},
            "color_scheme": "light",
        }
        self._browser_launch_opts = {
            # Merge stealth + visibility args; remove --enable-automation which
            # is Playwright's most identifiable bot fingerprint.
            "args": _stealth_args + _resource_args + _window_args,
            "ignore_default_args": ["--enable-automation", "--disable-gpu"],
        }
        self._persistent_context_opts = {
            **self._task_context_opts,
            **self._browser_launch_opts,
        }
        _common_context_opts = self._persistent_context_opts

        self._playwright = await async_playwright().start()

        profile_root = self._profile_root_for_user(user_id)
        profile_root.mkdir(parents=True, exist_ok=True)
        viola_profile = str(profile_root)

        if mode == "ephemeral":
            # Mode 1: Temp profile, wiped after session
            import tempfile

            temp_root = profile_root / "temp"
            temp_root.mkdir(parents=True, exist_ok=True)
            self._ephemeral_dir = tempfile.mkdtemp(prefix="session_", dir=str(temp_root))
            logger.info("Browser mode: ephemeral, profile=%s", self._ephemeral_dir)
            self._browser = None
            self._context = await self._playwright.chromium.launch_persistent_context(
                self._ephemeral_dir,
                headless=headless,
                **_common_context_opts,
            )
        else:
            # Mode 2 (default): Viola's built-in Chromium with persistent context.
            # Cookies and logins are saved between sessions.
            logger.info("Browser mode: viola, profile=%s", viola_profile)
            self._browser = None
            self._context = await self._playwright.chromium.launch_persistent_context(
                viola_profile,
                headless=headless,
                **_common_context_opts,
            )

        # Track which mode was used so ensure_browser() can detect changes.
        self._launched_mode = mode
        self._active_user_id = user_id

        # Defense-in-depth SSRF gate on the primary context (mirrors guard
        # installed on per-task contexts in _create_task_session).
        if self._context is not None:
            with suppress(Exception):
                await self._install_network_guard(self._context)

        # Apply playwright-stealth to hide all Playwright/automation fingerprints.
        # playwright-stealth covers ~20 detection vectors: navigator.webdriver,
        # chrome runtime object, plugin list, permissions, WebGL, hairline pixel,
        # and more — much more thorough than a manual init script.
        if self._context:
            try:
                from playwright_stealth import Stealth

                await Stealth().apply_stealth_async(self._context)
                logger.debug("playwright-stealth applied to browser context")
            except ImportError:
                # playwright-stealth not installed — fall back to minimal manual script.
                # Install with: pip install playwright-stealth
                logger.debug("playwright-stealth not available, using fallback init script")
                await self._context.add_init_script("""
                    // Hide navigator.webdriver — primary Playwright fingerprint
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined,
                        configurable: true,
                    });
                    // Simulate Chrome environment
                    if (!window.chrome) {
                        window.chrome = {
                            runtime: {},
                            loadTimes: function() {},
                            csi: function() {},
                            app: {},
                        };
                    }
                    // Restore realistic plugin + language lists
                    Object.defineProperty(navigator, 'plugins', {
                        get: function() {
                            var arr = [1, 2, 3, 4, 5];
                            arr.item = function(i) { return arr[i]; };
                            arr.namedItem = function(n) { return null; };
                            arr.refresh = function() {};
                            return arr;
                        },
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en'],
                    });
                    """)

        # For CDP mode, Chrome may already have pages (profile picker, etc.)
        # Use an existing page if available, otherwise create a new one.
        existing = [p for p in self._context.pages if not p.is_closed()] if self._context else []
        if existing:
            self._page = existing[0]
        elif self._context is not None:
            self._page = await self._context.new_page()
        if self._page is not None:
            self._page.on("dialog", lambda d: asyncio.ensure_future(_auto_handle_dialog(d)))
            # S7-05 multi-tenant: bind the launching user so captured responses
            # stay tenant-scoped.
            _launch_user = user_id
            self._page.on(
                "response",
                lambda resp: asyncio.ensure_future(self._on_response(resp, user_id=_launch_user)),
            )

    async def _close_internal(self) -> None:
        """Close everything (caller must hold the lock)."""
        self._launched_mode = None
        self._active_user_id = None
        self._active_pool_key = None
        self._api_log_by_user.clear()
        self._ref_map.clear()
        self._pool_ref_maps.clear()
        legacy_page = self._page
        legacy_context = self._context
        await self._close_all_task_sessions(merge_auth=True)

        if legacy_page is not None:
            try:
                await legacy_page.close()
            except Exception:
                logger.debug("Page close failed during shutdown")
            self._page = None

        if legacy_context is not None:
            try:
                await legacy_context.close()
            except Exception:
                logger.debug("Browser context close failed during shutdown")
            self._context = None

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("Browser close failed during shutdown")
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed during shutdown")
            self._playwright = None

        # Clean up ephemeral profile directory (mode 1)
        if self._ephemeral_dir:
            import shutil

            shutil.rmtree(self._ephemeral_dir, ignore_errors=True)
            self._ephemeral_dir = None
