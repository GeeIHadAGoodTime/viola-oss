"""The desktop companion client -- counterpart to ``services.companion.bridge``.

Lifecycle
---------
``CompanionClient.start()`` launches one supervisor task. That task:

0. **Resolves the account credential**, fresh, on every pass -- see
   :mod:`services.companion_client.config`. Prod GoTrue access tokens live
   300 seconds, so this is not a startup step that can be done once.
1. **Registers** the desktop as a companion device of the user's cloud
   account (``POST /api/v1/companion/register``), persisting the returned
   ``device_id`` + ``device_token`` via :class:`CompanionIdentityStore`.
   A stored identity is reused across restarts and across token refreshes;
   registration runs the first time, and again after the cloud revokes the
   device. A desktop that is merely signed out waits for sign-in rather than
   giving up.
2. **Connects** to ``/ws/companion/{device_id}`` with the two-factor
   handshake (:mod:`services.companion.ws_auth_contract`): the user-session
   credential in ``Authorization`` plus the device token in
   ``X-Companion-Device-Token``.
3. Runs the **dispatch loop**: every cloud command frame is handed to the
   :class:`CapabilityDispatcher`; the result (or error) is sent back keyed
   by ``request_id``.
4. Sends a periodic **heartbeat** (``system.health_check``) plus an initial
   ``system.capabilities_report`` + ``system.version_info`` so the cloud
   knows what the desktop can do.
5. **Reconnects** with exponential backoff if the socket drops; the cloud
   bridge re-drains any commands queued while offline.

Fail-safe posture
-----------------
* A handler that raises, returns ``{"error": ...}``, or is missing -> an
  ``error`` frame, never a crashed connection.
* An unknown / non-scoped command -> refused with an ``error`` frame.
* If the credential is missing, the client waits for a sign-in; it never
  guesses and never falls back to a global identity.
* ``stop()`` cancels everything and closes the socket cleanly.

The client holds NO module-level mutable per-user state; the singleton is a
single desktop's single client. It is provider-agnostic over the WebSocket
transport (``websockets`` is the only hard dependency).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from typing import Any

import httpx

# Explicit submodule import, not `import websockets` + `websockets.exceptions.X`.
# The top-level `websockets` package (>=14) resolves attributes like `connect`
# lazily via `__getattr__` (websockets/imports.py) and that lazy resolver does
# NOT alias the `exceptions` submodule itself -- `websockets.exceptions.X` only
# works if some OTHER import in the process already touched
# `websockets.exceptions` first (which registers it as an attribute of the
# parent package as a Python import-system side effect). This file happened to
# self-heal because `from websockets.asyncio.client import connect` below
# transitively imports `websockets.exceptions` as a side effect before the
# except clause at the bottom of this module is ever reached -- but that is
# accidental, not structural, and the exact anti-pattern that crashed
# `telephony/phone_cloud_event_relay.py` with
# `AttributeError: module 'websockets' has no attribute 'exceptions'` (#1529)
# the moment that module's import order changed. Importing the submodule
# directly is correct on every websockets version because it does not depend
# on load order (see test_client_module_imports_standalone, #3498).
import websockets.exceptions
from websockets.asyncio.client import ClientConnection, connect

from core.constants import TIMEOUT_EXTENDED, TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from services.companion.protocol import (
    AGENT_DISPATCH_TYPE,
    RELAY_TURN_DESKTOP_BUDGET_SECONDS,
    CompanionMessage,
    is_response_type,
    message_scope,
    normalize_message_type,
)
from services.companion.ws_auth_contract import (
    build_companion_ws_auth_headers,
    companion_ws_origin,
)

from .capabilities import CapabilityDispatcher
from .config import (
    CompanionClientConfig,
    CredentialProvider,
    companion_account_key,
    load_companion_client_config,
)
from .handlers import build_default_dispatcher
from .identity_store import CompanionIdentity, CompanionIdentityStore

logger = get_logger(__name__)

# Heartbeat cadence -- well under the bridge's tolerance; the bridge updates
# last-seen on every frame so any traffic also counts.
_HEARTBEAT_INTERVAL = 30.0
# Reconnect backoff bounds (seconds).
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0
# How often to re-check for a desktop sign-in while signed out. Deliberately
# flat rather than exponential: being signed out is the normal pre-pairing
# state, not a failure, and an exponential backoff would leave a user who
# just signed in waiting up to a minute for their desktop to appear.
_SIGNED_OUT_POLL_SECONDS = 15.0
# Per-command handler timeout -- a stuck handler must not wedge the loop.
# Bounded capability handlers (files.file_list, desktop.screenshot, ...) answer
# one small question, so the generic timeout is the right size for them.
_HANDLER_TIMEOUT = TIMEOUT_VERY_LONG
# A relayed WHOLE TURN is not a bounded call: it runs the user's entire request
# through the desktop's own agent pipeline. It gets the relay budget instead --
# see services/companion/protocol.py for why the two deadlines differ and why
# this one must stay BELOW the cloud's patience.
_RELAY_TURN_TIMEOUT = RELAY_TURN_DESKTOP_BUDGET_SECONDS


def _handler_timeout_for(message_type: str) -> float:
    """Budget for one command, by kind.

    ``asyncio.wait_for`` does not merely stop waiting -- it CANCELS the
    coroutine it is waiting on. For a relayed turn that coroutine is the whole
    desktop agent run, so an undersized budget here does not just lose the
    answer, it kills a turn that is already touching the user's machine.
    """
    try:
        normalized = normalize_message_type(message_type)
    except ValueError:
        return _HANDLER_TIMEOUT
    if normalized == AGENT_DISPATCH_TYPE:
        return _RELAY_TURN_TIMEOUT
    return _HANDLER_TIMEOUT


_CLIENT_SINGLETON: CompanionClient | None = None


class CompanionClient:
    """Maintains the desktop's live companion link to the cloud bridge."""

    def __init__(
        self,
        *,
        config: CompanionClientConfig,
        dispatcher: CapabilityDispatcher | None = None,
        identity_store: CompanionIdentityStore | None = None,
        connect_factory: Any | None = None,
    ) -> None:
        self._config = config
        self._dispatcher = dispatcher or build_default_dispatcher()
        self._identity_store = identity_store or CompanionIdentityStore()
        # Injectable for tests: a callable with the same shape as
        # ``websockets.asyncio.client.connect``.
        self._connect_factory = connect_factory or connect

        self._supervisor: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._connection: ClientConnection | None = None
        self._identity: CompanionIdentity | None = None
        self._connected = asyncio.Event()
        # In-flight command handler tasks, drained before the socket closes.
        self._inflight: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ state

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and not self._supervisor.done()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def device_id(self) -> str | None:
        return self._identity.device_id if self._identity is not None else None

    def advertised_capabilities(self) -> dict[str, Any]:
        return self._dispatcher.advertised_capabilities()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> bool:
        """Start the client. Returns ``True`` if a supervisor was launched."""
        if not self._config.is_runnable:
            logger.info(
                "Companion client idle: enabled=%s cloud_url=%s credential_source=%s",
                self._config.enabled,
                bool(self._config.cloud_url),
                self._config.has_credential_source,
            )
            return False
        if self.is_running:
            return True
        self._stopping.clear()
        self._supervisor = asyncio.create_task(self._run_supervisor(), name="companion-client")
        logger.info("Companion client started (cloud_url=%s)", self._config.cloud_url)
        return True

    async def stop(self) -> None:
        """Stop the client and close the connection cleanly."""
        self._stopping.set()
        connection = self._connection
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close(code=1000, reason="desktop_shutdown")
        for task in (*self._inflight, self._heartbeat_task, self._supervisor):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._inflight.clear()
        self._heartbeat_task = None
        self._supervisor = None
        self._connection = None
        self._connected.clear()
        logger.info("Companion client stopped")

    # ----------------------------------------------------------- supervisor

    async def _run_supervisor(self) -> None:
        """Pair and stay paired: resolve, register, connect, repeat until stopped.

        EVERY step lives inside the retry loop, including credential
        resolution and registration. That is the whole point of this shape:

        * The credential is a GoTrue access token with a 300-second life
          (``GOTRUE_JWT_EXP=300`` on prod), so it MUST be re-resolved -- and
          refreshed -- for each registration and each reconnect. A token
          captured once was expired before the second attempt, which made the
          ``_SessionRejected`` "reconnect with a refreshed credential" branch
          below unreachable by construction: it reconnected forever with the
          same dead string.
        * Registration used to run ONCE, before the loop, and ``return`` on
          any failure. So a desktop that was merely signed out at startup --
          or that hit one transient 5xx -- went dark until the whole app was
          restarted, with no path back. Now a signed-out desktop simply waits,
          and pairs the moment the user signs in.
        """
        backoff = _BACKOFF_MIN
        force_register = False
        announced_wait = False

        while not self._stopping.is_set():
            delay: float | None = None
            try:
                credential = await self._config.current_credential()
                if not credential:
                    if not announced_wait:
                        logger.info("Companion idle: waiting for a signed-in Viola account on this desktop")
                        announced_wait = True
                    raise _NotSignedIn("no desktop account session")
                announced_wait = False
                account_key = self._account_key_for(credential)

                self._identity = await self._ensure_registered(
                    credential=credential,
                    account_key=account_key,
                    force=force_register,
                )
                force_register = False
                await self._connect_and_serve(
                    self._identity,
                    credential=credential,
                    account_key=account_key,
                )
                backoff = _BACKOFF_MIN  # clean disconnect -> reset backoff
            except _NotSignedIn:
                # Not a failure. Poll on a steady, short cadence instead of an
                # exponential one so pairing comes up promptly after sign-in
                # rather than up to a minute later.
                delay = _SIGNED_OUT_POLL_SECONDS
                backoff = _BACKOFF_MIN
            except _DeviceRejected as exc:
                # The cloud no longer trusts this device token. Drop the
                # stored identity and re-register on the next loop -- with a
                # freshly resolved credential, and with backoff, so a cloud
                # that keeps refusing cannot spin a hot registration loop.
                logger.warning("Companion device rejected by cloud (%s); re-registering", exc)
                with contextlib.suppress(Exception):
                    self._identity_store.clear(
                        cloud_url=self._config.cloud_url,
                        account_key=self._account_key_for(await self._config.current_credential()),
                    )
                force_register = True
            except _SessionRejected as exc:
                # The user session was rejected (expired / revoked). Keep the
                # device identity; reconnect so the refreshed credential the
                # next loop resolves can re-establish the socket.
                logger.info("Companion user session rejected (%s); will reconnect", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Companion connection lost (%s); reconnecting", exc)

            if self._stopping.is_set():
                break
            if delay is None:
                # Exponential backoff with jitter so reconnect storms spread out.
                delay = min(_BACKOFF_MAX, backoff) * (0.5 + random.random())
                backoff = min(_BACKOFF_MAX, backoff * 2.0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    def _account_key_for(self, credential: str) -> str:
        """Identity-store partition key for the account behind ``credential``.

        Derived from the ACCOUNT (session identity, else the token's ``sub``
        claim), never from the token bytes -- a token-derived key changed on
        every GoTrue refresh, so the stored device identity became unfindable
        every five minutes and the desktop re-registered as a new device.
        """
        return companion_account_key(account_id=self._config.account_id, credential=credential)

    # ---------------------------------------------------------- registration

    async def _ensure_registered(
        self,
        *,
        credential: str | None = None,
        account_key: str | None = None,
        force: bool = False,
    ) -> CompanionIdentity:
        """Return a usable device identity, registering with the cloud if needed.

        ``credential`` / ``account_key`` are resolved by the supervisor and
        passed in so registration and the WebSocket handshake that follows use
        the SAME token; omit them and they are resolved here.
        """
        if credential is None:
            credential = await self._config.current_credential()
        if account_key is None:
            account_key = self._account_key_for(credential)

        if not force:
            existing = self._identity_store.load(
                cloud_url=self._config.cloud_url,
                account_key=account_key,
            )
            if existing is not None:
                logger.info("Reusing companion device identity %s", existing.to_log_dict())
                return existing

        register_url = "%s/api/v1/companion/register" % self._config.cloud_url
        body = {
            "device_name": self._config.device_name,
            "platform": self._config.platform,
            "capabilities": self._dispatcher.advertised_capabilities(),
        }
        headers = {"Authorization": "Bearer %s" % credential}

        async with httpx.AsyncClient(timeout=TIMEOUT_EXTENDED) as client:
            response = await client.post(register_url, json=body, headers=headers)
        if response.status_code == 401:
            raise _DeviceRejected("cloud rejected the account credential")
        response.raise_for_status()

        data = response.json()
        device = data.get("device") or {}
        device_id = str(device.get("id") or "").strip()
        device_token = str(data.get("device_token") or "").strip()
        if not device_id or not device_token:
            raise RuntimeError("Companion registration response missing device_id/device_token")

        return self._identity_store.save(
            cloud_url=self._config.cloud_url,
            account_key=account_key,
            device_id=device_id,
            device_token=device_token,
            device_name=str(device.get("device_name") or self._config.device_name),
            platform=str(device.get("platform") or self._config.platform),
        )

    # ------------------------------------------------------------ connection

    def _ws_url(self, identity: CompanionIdentity) -> str:
        base = self._config.cloud_url
        scheme = "wss" if base.startswith("https") else "ws"
        host = base.split("://", 1)[-1]
        return "%s://%s/ws/companion/%s" % (scheme, host, identity.device_id)

    async def _connect_and_serve(
        self,
        identity: CompanionIdentity,
        *,
        credential: str | None = None,
        account_key: str | None = None,
    ) -> None:
        """Open the WebSocket, run the dispatch loop, then clean up."""
        del account_key  # accepted for call-site symmetry with _ensure_registered
        ws_url = self._ws_url(identity)
        # Resolve the session credential HERE, not from a value captured when
        # the config was built: a GoTrue access token lives 300 seconds, so
        # every reconnect after the first would otherwise present a token the
        # cloud has already expired and close 4401 forever.
        if credential is None:
            credential = await self._config.current_credential()
        # Two-factor companion handshake (services.companion.ws_auth_contract):
        #   * Authorization: Bearer <user-session credential> -- the SAME cloud
        #     account credential used for POST /api/v1/companion/register.
        #   * X-Companion-Device-Token: <device token> -- proves THIS device.
        #   * Origin: <the cloud we are connecting to> -- checked BEFORE either
        #     credential; without it the cloud closes every companion socket
        #     1008 "Origin not allowed" and the desktop can never be online.
        # The server verifies the session (verify_websocket_auth) and then the
        # device token bound to that session's user (verify_device_for_user).
        headers = build_companion_ws_auth_headers(
            session_credential=credential,
            device_token=identity.device_token,
            origin=companion_ws_origin(self._config.cloud_url),
        )
        try:
            connection = await self._connect_factory(ws_url, additional_headers=headers)
        except websockets.exceptions.InvalidStatus as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            self._raise_for_auth_rejection(code, "handshake status %s" % code, exc)
            raise

        self._connection = connection
        self._connected.set()
        logger.info("Companion connected to %s", ws_url)
        try:
            # Announce what we can do as soon as the socket is up.
            await self._send_capabilities_report(connection)
            await self._send_version_info(connection)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(connection), name="companion-heartbeat")
            await self._dispatch_loop(connection)
        except websockets.exceptions.ConnectionClosed as exc:
            # The cloud rejects an unauthorized companion socket AFTER
            # completing the opening handshake, on purpose: per RFC 6455 a
            # rejected HTTP upgrade has no room for an app-chosen close
            # code, and uvicorn collapses any pre-accept close into a bare
            # 403 with the code and reason discarded. So
            # ``ui.core.security.reject_websocket`` accepts first and then
            # sends the real 4401/4403 close frame -- which means the auth
            # verdict arrives HERE, as a close code on an open socket, and
            # never as the InvalidStatus above.
            #
            # Reading the verdict only from InvalidStatus left both recovery
            # branches unreachable against the real server: a revoked device
            # token produced a generic "connection lost", the stored identity
            # was never cleared, and the desktop reconnect-looped forever on
            # a dead credential instead of re-registering -- permanently dark
            # to the phone, with no path back.
            close_code = getattr(getattr(exc, "rcvd", None), "code", None)
            self._raise_for_auth_rejection(close_code, "close code %s" % close_code, exc)
            raise
        finally:
            self._connected.clear()
            self._connection = None
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._heartbeat_task
                self._heartbeat_task = None
            # Let in-flight handlers finish replying before the socket closes
            # (a clean shutdown still cancels them via stop()).
            await self._drain_inflight()
            with contextlib.suppress(Exception):
                await connection.close()

    @staticmethod
    def _raise_for_auth_rejection(code: int | None, detail: str, cause: BaseException) -> None:
        """Map a companion auth rejection to the right recovery, or return.

        One mapping, applied to BOTH ways the verdict can arrive (a pre-accept
        HTTP status, or the post-accept close frame the cloud actually sends),
        so the two paths can never diverge again.
        """
        # 4403/403 = the DEVICE credential was rejected (revoked / wrong
        # token) -> drop the stored identity and re-register.
        #
        # 4001 is the same verdict arriving a different way, and it arrives
        # FIRST. When the cloud revokes a device it closes the live socket
        # itself with 4001 "revoked" (services/companion/bridge.py's
        # close_device default, used only by the revoke route -- the shutdown
        # caller passes 1001 explicitly), and only a SUBSEQUENT reconnect
        # attempt earns the 4403. Measured live against prod on 2026-08-06:
        # revoking a paired device produced `4001 revoked` -> generic
        # "connection lost" -> a wasted reconnect -> 4403 -> re-register. The
        # recovery converged, one round trip later than it needed to. Reading
        # 4001 as the device verdict it already is collapses that.
        if code in (4403, 403, 4001):
            raise _DeviceRejected(detail) from cause
        # 4401/401 = the USER SESSION was rejected (expired / revoked). The
        # device registration is still valid, so do NOT wipe it; reconnect
        # with backoff and pick up a refreshed credential. Fail closed: no
        # socket serves commands without a live session.
        if code in (4401, 401):
            raise _SessionRejected(detail) from cause

    async def _drain_inflight(self) -> None:
        """Wait for any in-flight command handler tasks to complete."""
        if not self._inflight:
            return
        pending = list(self._inflight)
        with contextlib.suppress(Exception):
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()

    async def _dispatch_loop(self, connection: ClientConnection) -> None:
        """Receive command frames and service each one."""
        async for raw in connection:
            if self._stopping.is_set():
                break
            if isinstance(raw, (bytes, bytearray)):
                # The desktop handlers are JSON today; binary command frames
                # (e.g. audio chunk upstream) are not yet serviced.
                logger.warning("Companion received an unsupported binary frame; ignoring")
                continue
            try:
                message = CompanionMessage.from_json(str(raw))
            except Exception:
                logger.warning("Companion received a malformed frame; ignoring")
                continue
            # Service each command concurrently so one slow handler does not
            # block the rest. Errors are contained per-task; the task is
            # tracked so a closing connection drains it first.
            task = asyncio.create_task(self._handle_command(connection, message))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _handle_command(self, connection: ClientConnection, message: CompanionMessage) -> None:
        """Run one command's handler and reply with a result/error frame."""
        # Response-typed frames are bridge->device acks, not commands.
        if is_response_type(message.type):
            return
        if not message.request_id:
            # A request_id-less system.* frame is a server-side probe with no
            # reply expected; anything else without a request_id is ignored.
            return

        try:
            if message_scope(message.type) is None:
                raise KeyError(message.type)
            payload = dict(message.payload)
            if message.type == "system.capabilities_report":
                payload["capabilities"] = self._dispatcher.advertised_capabilities()
            result = await asyncio.wait_for(
                self._dispatcher.dispatch(message.type, payload),
                timeout=_handler_timeout_for(message.type),
            )
        except KeyError:
            await self._send_error(connection, message.request_id, "Unsupported companion command")
            return
        except TimeoutError:
            await self._send_error(connection, message.request_id, "Desktop handler timed out")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Companion handler crashed for %s", message.type)
            await self._send_error(connection, message.request_id, "Desktop handler failed")
            return

        # A handler may surface an expected failure as {"error": ...}.
        if isinstance(result, dict) and "error" in result and len(result) == 1:
            await self._send_error(connection, message.request_id, str(result["error"]))
            return

        await self._send_result(connection, message.request_id, result)

    # --------------------------------------------------------------- senders

    async def _send_result(self, connection: ClientConnection, request_id: str, payload: dict[str, Any]) -> None:
        await self._send(
            connection,
            CompanionMessage(type="result", payload=payload, request_id=request_id),
        )

    async def _send_error(self, connection: ClientConnection, request_id: str, error: str) -> None:
        await self._send(
            connection,
            CompanionMessage(type="error", payload={"error": error}, request_id=request_id),
        )

    async def _send_capabilities_report(self, connection: ClientConnection) -> None:
        await self._send(
            connection,
            CompanionMessage(
                type="system.capabilities_report",
                payload={"capabilities": self._dispatcher.advertised_capabilities()},
            ),
        )

    async def _send_version_info(self, connection: ClientConnection) -> None:
        from core.constants import VIOLA_VERSION

        await self._send(
            connection,
            CompanionMessage(
                type="system.version_info",
                payload={
                    "app": "viola-desktop",
                    "version": VIOLA_VERSION,
                    "platform": self._config.platform,
                },
            ),
        )

    async def _send(self, connection: ClientConnection, message: CompanionMessage) -> None:
        """Serialize and send one message frame as JSON. Swallows send races.

        Binary frames (``CompanionBinaryFrame``, e.g. upstream audio chunks)
        are part of the protocol but not yet emitted by the desktop client.
        """
        try:
            await connection.send(message.to_json())
        except Exception:
            logger.debug("Companion send failed for %s (socket closing?)", message.type)

    async def _heartbeat_loop(self, connection: ClientConnection) -> None:
        """Emit a periodic health_check so the cloud keeps the device online."""
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=_HEARTBEAT_INTERVAL)
                return  # stop requested
            except TimeoutError:
                pass
            with contextlib.suppress(Exception):
                await self._send(
                    connection,
                    CompanionMessage(
                        type="system.health_check",
                        payload={"timestamp": time.time(), "status": "online"},
                    ),
                )


class _DeviceRejected(RuntimeError):
    """The cloud refused the device token (revoked / wrong device credential)."""


class _SessionRejected(RuntimeError):
    """The cloud refused the user session (expired / revoked); device is intact."""


class _NotSignedIn(RuntimeError):
    """No Viola account is signed in on this desktop yet -- wait, do not fail."""


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


async def build_companion_client(
    *,
    credential_provider: CredentialProvider | None = None,
) -> CompanionClient:
    """Construct (and cache) the process-wide companion client."""
    global _CLIENT_SINGLETON
    config = load_companion_client_config(credential_provider=credential_provider)
    client = CompanionClient(config=config)
    _CLIENT_SINGLETON = client
    return client


def get_companion_client() -> CompanionClient | None:
    """Return the process-wide companion client, or ``None`` if not built."""
    return _CLIENT_SINGLETON


def _reset_companion_client_singleton() -> None:
    """Test hook: drop the cached singleton."""
    global _CLIENT_SINGLETON
    _CLIENT_SINGLETON = None
