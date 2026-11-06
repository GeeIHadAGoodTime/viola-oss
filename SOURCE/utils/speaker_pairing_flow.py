"""Shared helpers for the desktop LAN speaker pairing flow.

LAN access (why the Add Room link can work at all)
--------------------------------------------------
The desktop API binds loopback (``config/settings.py`` ``api_host``), so a
clean install opens no LAN port and shows no firewall prompt over onboarding
(#4867). A phone can only follow the Add Room QR / link / pairing word if the
hub ALSO listens on its LAN address. That is the user's call, and it takes a
deliberate act: the Start button in Rooms > Add Room, the Settings toggle, or
asking Viola to pair a speaker. Those run ``ensure_lan_access(enable=True)``,
which remembers the choice in the install-scoped ``lan_access_enabled`` setting
and opens a ``core.server_factory.LanListener`` on the LAN address the link
shows. Merely LOOKING at Rooms opens nothing: the panel's
``/v1/network/local-address`` call only reports (1.0.5 build 9 opened the port
on every look, because Add Room is the modal's first tab, and kept it open for
good).

The port stays open only while it has a use. ``lan_access_supervisor`` turns
LAN access back off once nothing has paired for
``LAN_ACCESS_UNPAIRED_WINDOW_SECONDS`` after the last sign of someone pairing,
and at startup when no paired device is left to reconnect. That also closes
the port on installs an older build left open.

The listener serves the same app object, so the middleware auth, the
spoke-scope allowlist and every route-level loopback-only guard apply to LAN
callers exactly as before. If LAN access cannot be opened (auth disabled, an
explicit loopback ``VIOLA_API_HOST``, no network, the port taken), the payload
says why and carries no link, so the panel never shows a URL that cannot work.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import subprocess  # nosec B404 - fixed read-only PowerShell query, no shell
import sys
import threading
import time
import weakref
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from config.settings import settings
from core.constants import LOCALHOST
from core.json_types import JsonDict
from core.logging_config import get_logger
from ui.security.config import get_security_config
from ui.security.spoke_pairing_ticket import (
    PAIRING_TICKET_MAX_AGE_SECONDS,
    PAIRING_TICKET_QUERY_PARAM,
    issue_pairing_ticket,
)
from utils.pairing_codec import encode_ip

logger = get_logger(__name__)


def get_pairing_lan_ip() -> str:
    """Return the hub LAN IP used by the speaker pairing URL."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:
        logger.debug("Could not determine speaker pairing LAN IP, using localhost")
        return LOCALHOST


def build_spoke_url_prefix(ip: str, port: int) -> str:
    """Return the QR URL prefix used before appending the room slug.

    The URL carries a short-lived, single-use PAIRING TICKET, never a live
    spoke credential (#4434). The Add Room screen shows this URL twice — inside
    the QR image and, historically, as plain text beside it — so anything it
    carries is one screenshot or one camera away from being public. A ticket
    opens no audio socket by itself: the joining device exchanges it once, from
    the LAN, at ``POST /bootstrap/claim`` for a real credential that lands in
    its cookie jar and is never displayed anywhere.
    """
    scheme = "https" if settings.ssl_enabled else "http"
    base_url = "%s://%s:%s/" % (scheme, ip, port)
    if get_security_config().auth_enabled:
        ticket = issue_pairing_ticket()
        token = quote(ticket.token, safe="")
        return "%s?%s=%s&room=" % (base_url, PAIRING_TICKET_QUERY_PARAM, token)
    return "%s?room=" % base_url


def build_local_address_payload(lan_status: LanAccessStatus | None = None) -> JsonDict:
    """Return the same local-address payload used by the Add Room UI.

    With ``lan_status`` (the panel route passes the result of
    ``ensure_lan_access``), the link, QR target and pairing word are only
    produced when a phone can actually reach that address. Otherwise the
    payload explains why in ``lan_access`` and carries no link and no ticket.
    """
    if lan_status is not None and not lan_status.reachable:
        return {
            "ip": lan_status.host or "",
            "port": int(lan_status.port or settings.api_port),
            "spoke_url": "",
            "pairing_code": "",
            "lan_access": lan_status.to_dict(),
        }
    ip = lan_status.host if lan_status is not None and lan_status.host else get_pairing_lan_ip()
    port = int(settings.api_port)
    payload: JsonDict = {
        "ip": ip,
        "port": port,
        "spoke_url": build_spoke_url_prefix(ip, port),
        "pairing_code": encode_ip(ip),
        # The panel masks the ticket and re-fetches before it lapses, so the QR
        # on screen is always redeemable while the user is looking at it.
        "pairing_ticket_param": PAIRING_TICKET_QUERY_PARAM,
        "pairing_ticket_expires_in": PAIRING_TICKET_MAX_AGE_SECONDS,
    }
    if lan_status is not None:
        payload["lan_access"] = lan_status.to_dict()
    return payload


# ---------------------------------------------------------------------------
# LAN access: the extra listener a phone needs to reach the hub
# ---------------------------------------------------------------------------

LAN_ACCESS_SETTING = "lan_access_enabled"
LAN_ACCESS_SUPERVISOR_INTERVAL_SECONDS = 15.0
# How long LAN access stays open with nothing paired, counted from the last
# sign of someone pairing (Start, or the open panel renewing its link every few
# minutes). Long enough to find a phone and scan the code, short enough that a
# forgotten Start does not leave a port open on the Wi-Fi all week.
LAN_ACCESS_UNPAIRED_WINDOW_SECONDS = 15 * 60
_FIREWALL_CACHE_SECONDS = 30.0


@dataclass(frozen=True)
class LanAccessStatus:
    """Whether a phone on the Wi-Fi can reach this hub, and if not, why."""

    enabled: bool
    reachable: bool
    host: str | None
    port: int
    via: str  # "lan_listener" | "main_bind" | "none"
    reason_code: str | None = None
    message: str | None = None
    firewall: JsonDict | None = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


_lan_app: Any = None
_lan_listener: Any = None
_lan_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_lan_locks_guard = threading.Lock()
_firewall_cache: tuple[float, str, JsonDict] | None = None
# time.monotonic() deadline for pairing something; None until a user start in
# this run, which is how a setting left on by an earlier run is recognised.
_lan_unpaired_deadline: float | None = None


def register_lan_app(app: Any) -> None:
    """Remember the ASGI app the LAN listener must serve (desktop startup)."""
    global _lan_app
    _lan_app = app


def _lan_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _lan_locks_guard:
        lock = _lan_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _lan_locks[loop] = lock
        return lock


def _is_loopback_host(host: str | None) -> bool:
    value = (host or "").strip().lower()
    if not value or value.rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_wildcard_host(host: str | None) -> bool:
    return (host or "").strip() in ("0.0.0.0", "::")  # nosec B104 - comparison only


def _explicit_loopback_bind() -> bool:
    """True when the operator pinned the API to loopback via the environment."""
    explicit = os.environ.get("VIOLA_API_HOST")
    if explicit is None:
        explicit = os.environ.get("VIOLA_HOST")
    return explicit is not None and _is_loopback_host(explicit)


def _lan_access_setting_enabled() -> bool:
    try:
        from config.facade import get_settings_manager

        return bool(get_settings_manager().get(LAN_ACCESS_SETTING, False))
    except Exception:
        logger.debug("Could not read %s; treating LAN access as off", LAN_ACCESS_SETTING, exc_info=True)
        return False


def _persist_lan_access_setting(enabled: bool) -> None:
    try:
        from config.facade import get_settings_manager

        manager = get_settings_manager()
        if bool(manager.get(LAN_ACCESS_SETTING, False)) != enabled:
            manager.set(LAN_ACCESS_SETTING, enabled)
    except Exception:
        logger.warning("Could not save %s=%s", LAN_ACCESS_SETTING, enabled, exc_info=True)


def _has_paired_device() -> bool:
    """True when a device could still reconnect to the hub over the LAN."""
    try:
        from ui.security.spoke_credentials import SPOKE_TOKEN_IDLE_MAX_AGE_SECONDS
        from ui.security.spoke_device_registry import list_devices

        oldest_usable = int(time.time()) - SPOKE_TOKEN_IDLE_MAX_AGE_SECONDS
        return any(
            not device.get("revoked") and int(device.get("last_seen") or 0) >= oldest_usable
            for device in list_devices()
        )
    except Exception:
        # A read failure must not unpair a working house.
        logger.warning("Could not read paired devices; leaving LAN access as it is", exc_info=True)
        return True


def note_pairing_activity() -> None:
    """Someone is pairing right now: give them the full window again."""
    global _lan_unpaired_deadline
    _lan_unpaired_deadline = time.monotonic() + LAN_ACCESS_UNPAIRED_WINDOW_SECONDS


async def _retire_unused_lan_access(at_startup: bool = False) -> bool:
    """Turn LAN access off when nothing uses it; return True if it did.

    At startup a saved "on" with nothing paired is closed straight away, since
    no device is waiting to reconnect. Later the user gets the pairing window.
    """
    if not await asyncio.to_thread(_lan_access_setting_enabled):
        return False
    if await asyncio.to_thread(_has_paired_device):
        return False
    deadline = _lan_unpaired_deadline
    if deadline is None:
        if not at_startup:
            # Turned on outside this module (a settings file edit, say).
            note_pairing_activity()
            return False
    elif time.monotonic() < deadline:
        return False
    logger.info(
        "LAN access closed: no device paired (%s)",
        "left on by an earlier run" if deadline is None else "pairing window ended",
    )
    await ensure_lan_access(False, check_firewall=False)
    return True


def _lan_blocked_reason() -> tuple[str, str] | None:
    """Return why LAN access must stay closed, or ``None`` when it may open."""
    if str(getattr(settings, "app_surface", "desktop")).lower() != "desktop":
        return ("not_desktop", "Rooms pairing runs on the desktop app.")
    if _explicit_loopback_bind():
        return (
            "loopback_pinned",
            "Phones can't reach this computer because VIOLA_API_HOST pins Viola to this computer only.",
        )
    if not get_security_config().auth_enabled:
        # Same rule as the startup guard in config/settings_validation.py:
        # nothing listens on the LAN while API authentication is off.
        return (
            "auth_disabled",
            "Phones can't connect while Viola's security checks are turned off on this computer.",
        )
    return None


def _main_bind_lan_host(lan_ip: str) -> str | None:
    """Return the LAN host the MAIN server already reaches, if any."""
    api_host = str(getattr(settings, "api_host", "") or "")
    if _is_wildcard_host(api_host):
        return lan_ip if not _is_loopback_host(lan_ip) else None
    if api_host and not _is_loopback_host(api_host):
        return api_host
    return None


def _firewall_powershell_script(program: str) -> str:
    quoted = program.replace("'", "''")
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        "$r=@(Get-NetFirewallApplicationFilter -Program '%s' | Get-NetFirewallRule |"
        " Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' } |"
        " Select-Object @{n='action';e={[string]$_.Action}},@{n='profile';e={[string]$_.Profile}});"
        "$p=@(Get-NetConnectionProfile | Select-Object @{n='category';e={[string]$_.NetworkCategory}},"
        "@{n='alias';e={[string]$_.InterfaceAlias}});"
        "@{rules=$r;profiles=$p} | ConvertTo-Json -Compress -Depth 4" % quoted
    )


def _profile_covers(profile: str, category: str) -> bool:
    parts = {part.strip().lower() for part in str(profile or "").split(",")}
    return "any" in parts or category.lower() in parts


def classify_firewall_state(rules: list[dict[str, Any]], categories: list[str]) -> JsonDict:
    """Turn the program's inbound rules into a state the panel can explain.

    Windows Firewall blocks inbound traffic by default, and a Block rule wins
    over an Allow rule. Dismissing the "allow access" prompt writes a Block
    rule for the program, which would otherwise fail silently forever.
    """
    lowered = [c.lower() for c in categories if c]
    if lowered and all(c == "private" for c in lowered):
        category = "Private"
    elif "public" in lowered:
        category = "Public"
    else:
        category = categories[0] if categories else ""
    on_public_network = category.lower() == "public"
    blocked = any(
        str(rule.get("action", "")).lower() == "block" and _profile_covers(str(rule.get("profile", "")), category)
        for rule in rules
    )
    allowed = any(
        str(rule.get("action", "")).lower() == "allow" and _profile_covers(str(rule.get("profile", "")), category)
        for rule in rules
    )
    if blocked:
        state = "blocked"
        message = (
            "Windows Firewall is blocking Viola on this network. Open Windows Security, go to "
            "Firewall & network protection, choose 'Allow an app through firewall', and allow Viola."
        )
    elif allowed:
        state = "allowed"
        # Viola's installer adds no firewall rule. An Allow on Public came from
        # someone answering the Windows prompt on a public network, and the
        # app cannot change it without admin rights, so say what it means.
        message = (
            (
                "Windows treats this Wi-Fi as a public network and allows Viola on it, so other people here "
                "can reach this computer's Rooms page. They still need your code. On your own Wi-Fi, set it "
                "to Private in Windows network settings. Elsewhere, stop Rooms when you're done."
            )
            if on_public_network
            else None
        )
    else:
        state = "no_rule"
        message = "If Windows asks whether Viola may use this network, choose Allow."
        if on_public_network:
            message = (
                "Windows treats this Wi-Fi as a public network. If this is your own Wi-Fi, set it to Private "
                "in Windows network settings, then choose Allow if Windows asks about Viola."
            )
    return {"state": state, "network_category": category or None, "message": message}


def check_windows_firewall(program: str | None = None) -> JsonDict | None:
    """Read (never change) the inbound firewall rules for this app on Windows.

    Returns ``None`` off Windows. Cached briefly because the panel re-fetches
    its pairing ticket every few minutes. Runs PowerShell without a console
    window; any failure reports ``state="unknown"`` rather than guessing.
    """
    global _firewall_cache
    if sys.platform != "win32":
        return None
    program = program or sys.executable
    now = time.monotonic()
    cached = _firewall_cache
    if cached is not None and cached[1] == program and now - cached[0] < _FIREWALL_CACHE_SECONDS:
        return cached[2]
    result: JsonDict = {"state": "unknown", "network_category": None, "message": None}
    try:
        completed = subprocess.run(  # nosec B603 B607 - fixed read-only query, no shell
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _firewall_powershell_script(program)],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(completed.stdout or "{}")
        rules = data.get("rules") or []
        profiles = data.get("profiles") or []
        if isinstance(rules, dict):
            rules = [rules]
        if isinstance(profiles, dict):
            profiles = [profiles]
        categories = [
            str(item.get("category", ""))
            for item in profiles
            if isinstance(item, dict) and not str(item.get("alias", "")).lower().startswith("vethernet")
        ]
        result = classify_firewall_state([r for r in rules if isinstance(r, dict)], categories)
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.debug("Windows Firewall rule check failed", exc_info=True)
    _firewall_cache = (now, program, result)
    return result


async def _stop_lan_listener_locked() -> None:
    global _lan_listener
    from core.lan_exposure import set_active_lan_listener

    listener, _lan_listener = _lan_listener, None
    set_active_lan_listener(None)
    if listener is not None:
        await listener.stop()


def _off_status(enabled: bool, port: int, code: str, message: str, host: str | None = None) -> LanAccessStatus:
    return LanAccessStatus(enabled, False, host, port, "none", code, message)


async def ensure_lan_access(enable: bool | None = None, *, check_firewall: bool = True) -> LanAccessStatus:
    """Open, keep, move or close the LAN listener to match the user's choice.

    ``enable=True`` records the user's request (Start in Add Room, the
    Settings toggle, asking Viola to pair a speaker), ``enable=False`` turns
    LAN access off, ``None`` only reconciles with the saved setting and is
    safe for anything that merely looks. Must run on the loop serving the app.
    Never raises; failures come back in the status.
    """
    from core.lan_exposure import set_active_lan_listener
    from core.server_factory import LanListener, LanListenerError

    global _lan_listener, _lan_unpaired_deadline
    port = int(settings.api_port)
    async with _lan_lock():
        if enable is not None:
            await asyncio.to_thread(_persist_lan_access_setting, bool(enable))
            enabled = bool(enable)
            if enabled:
                note_pairing_activity()
            else:
                _lan_unpaired_deadline = None
        else:
            enabled = await asyncio.to_thread(_lan_access_setting_enabled)

        blocked = _lan_blocked_reason()
        if blocked is not None:
            await _stop_lan_listener_locked()
            return _off_status(enabled, port, blocked[0], blocked[1])

        lan_ip = await asyncio.to_thread(get_pairing_lan_ip)
        main_host = _main_bind_lan_host(lan_ip)
        if main_host is not None:
            # VIOLA_ENABLE_MULTIROOM=1 / explicit LAN bind: the main server
            # already listens there; an extra listener would only collide.
            await _stop_lan_listener_locked()
            firewall = await asyncio.to_thread(check_windows_firewall) if check_firewall else None
            return LanAccessStatus(enabled, True, main_host, port, "main_bind", firewall=firewall)

        if not enabled:
            await _stop_lan_listener_locked()
            return _off_status(
                False,
                port,
                "lan_access_off",
                "Phones can't connect: letting devices on your Wi-Fi reach Viola is turned off.",
            )

        if _is_loopback_host(lan_ip):
            await _stop_lan_listener_locked()
            return _off_status(
                True,
                port,
                "no_network",
                "This computer isn't on a network right now. Connect it to Wi-Fi and try again.",
            )

        if _lan_app is None:
            return _off_status(True, port, "not_ready", "Viola is still starting. Try again in a moment.", lan_ip)

        listener = _lan_listener
        if listener is None or not listener.running or listener.host != lan_ip or listener.port != port:
            await _stop_lan_listener_locked()
            listener = LanListener(_lan_app, lan_ip, port)
            try:
                await listener.start()
            except LanListenerError as exc:
                logger.warning("LAN access could not open: %s", exc)
                return _off_status(
                    True,
                    port,
                    "listen_failed",
                    "Viola couldn't open port %d on %s for other devices. Another app may be using it."
                    % (port, lan_ip),
                    lan_ip,
                )
            _lan_listener = listener
            set_active_lan_listener(lan_ip, port)

        firewall = await asyncio.to_thread(check_windows_firewall) if check_firewall else None
        return LanAccessStatus(True, True, lan_ip, port, "lan_listener", firewall=firewall)


async def stop_lan_access_listener() -> None:
    """Close the LAN listener without changing the saved setting (shutdown)."""
    async with _lan_lock():
        await _stop_lan_listener_locked()


async def lan_access_supervisor(interval: float = LAN_ACCESS_SUPERVISOR_INTERVAL_SECONDS) -> None:
    """Keep the listener in line with the setting, the LAN address and Settings.

    Re-runs ``ensure_lan_access()`` periodically: brings a remembered LAN
    listener back after a restart once the network is up, follows a DHCP
    address change, and applies a toggle saved from Settings. Before that it
    closes LAN access that nothing paired with. Cheap: reads one setting, and
    only when that is on the paired-device list, then asks the OS for the
    outbound interface address.
    """
    at_startup = True
    while True:
        try:
            await _retire_unused_lan_access(at_startup)
            at_startup = False
            await ensure_lan_access(None, check_firewall=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("LAN access supervisor pass failed", exc_info=True)
        await asyncio.sleep(interval)


def request_lan_access() -> None:
    """Ask the running desktop server to open LAN access (sync callers).

    Used by the voice/LLM "pair a speaker" flow, which opens the Add Room panel
    (the panel's own fetch confirms reachability). No-op outside a running
    desktop server, so tests and the cloud never touch settings or sockets.
    """
    if _lan_app is None:
        return
    try:
        from core.asyncio_safe import get_main_loop

        loop = get_main_loop()
    except ImportError:
        loop = None
    if loop is None or loop.is_closed():
        return
    future = asyncio.run_coroutine_threadsafe(ensure_lan_access(True, check_firewall=False), loop)
    future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)


def room_label(target_room: str) -> str:
    """Return a normalized room label for structured routing metadata."""
    return target_room.strip().strip("'\"") or "room"


def build_speaker_pairing_flow(target_room: str) -> JsonDict:
    """Build UI metadata that opens the existing in-house speaker pairing QR flow."""
    request_lan_access()
    local_address = build_local_address_payload()
    room_slug = quote((target_room.strip() or "speaker"), safe="")
    spoke_url_prefix = str(local_address["spoke_url"])
    spoke_url = "%s%s" % (spoke_url_prefix, room_slug)
    base_url = spoke_url_prefix.split("/?", 1)[0]
    pairing_code = str(local_address["pairing_code"])
    return {
        "action": "pair_speaker",
        "available": True,
        "path_identifier": "rooms.add_speaker",
        "ui_action": "open_rooms_add_speaker",
        "target_room": target_room,
        "rooms_modal_tab": "add-speaker",
        "connect_page_url": "%s/connect" % base_url,
        "spoke_url": spoke_url,
        "qr_url": spoke_url,
        "qr_endpoint_url": spoke_url,
        "pairing_code": pairing_code,
        "qr_available": True,
        "qr_location": "Rooms > Add Room",
    }


def build_speaker_pairing_payload(target_room: str) -> JsonDict:
    """Return the structured setup envelope consumed by UI and LLM responses."""
    room_name = room_label(target_room)
    pairing_flow = build_speaker_pairing_flow(target_room)
    qr_data = {
        "payload": pairing_flow["spoke_url"],
        "qr_url": pairing_flow["qr_url"],
        "spoke_url": pairing_flow["spoke_url"],
        "connect_page_url": pairing_flow["connect_page_url"],
        "pairing_code": pairing_flow["pairing_code"],
        "room_name": room_name,
    }
    return {
        "message": "speaker_pairing_flow_available",
        "target_room": target_room,
        "room_name": room_name,
        "prefill": {"room_name": room_name},
        "paired": False,
        "pairing_flow_available": True,
        "pairing_flow": pairing_flow,
        "qr_data": qr_data,
        "pairing_code": pairing_flow["pairing_code"],
        "spoke_url": pairing_flow["spoke_url"],
        "ui_action": pairing_flow["ui_action"],
        "rooms_modal_tab": pairing_flow["rooms_modal_tab"],
    }
