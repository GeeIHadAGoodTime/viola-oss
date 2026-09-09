"""``HttpSyncTransport`` — the httpx implementation of the Tier-2 ``SyncTransport``.

Split out from ``services/sync/client.py`` so the pure client logic carries no hard
httpx dependency (tests inject an in-process fake transport). This class speaks the
shipped ``/v1/sync/*`` wire contract:

* ``Authorization: Bearer <token>`` (the desktop GoTrue access token, fetched fresh
  per request via an async ``token_provider`` so a rotated token is always used) and
  ``X-Device-Id`` on every request.
* Parses the ``{ok, data, error}`` envelope (``contracts.api_response``).
* Maps a ``403 consent_required`` to ``SyncConsentRequired`` so the client treats it
  as a clean no-op; any other error envelope / transport failure becomes
  ``SyncTransportError``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from core.logging_config import get_logger
from services.sync.client import (
    PullResponse,
    PushResponse,
    SyncConsentRequired,
    SyncTransportError,
    TokenProvider,
)

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0

# The shipped per-key Tier-2 user_preferences route
# (``ui/api/routes/cloud_sync_user.py::put_user_preference``). Kept as a module
# constant because it is the desktop's half of a client/server wire contract:
# ``scripts/check_desktop_consent_server_contract.py`` reads this string and
# proves the server actually declares a route of that shape, so the desktop can
# never drift into calling a path the server does not serve (the C-708 bug
# class, found on iOS first).
USER_PREFERENCE_ROUTE_TEMPLATE = "/v1/sync/user-preferences/{key}"


class HttpSyncTransport:
    """httpx-backed transport for the desktop Tier-2 sync client."""

    def __init__(
        self,
        *,
        base_url: str,
        device_id: str,
        token_provider: TokenProvider,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._device_id = str(device_id or "").strip()
        if not self._device_id:
            raise ValueError("device_id must be non-empty")
        self._token_provider = token_provider
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpSyncTransport:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _headers(self) -> dict[str, str]:
        token = await self._token_provider()
        if not token:
            # No bearer => not signed in. Fail closed as a consent/auth no-op rather
            # than sending an unauthenticated write.
            raise SyncConsentRequired("No desktop access token available")
        return {"Authorization": "Bearer %s" % token, "X-Device-Id": self._device_id}

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise SyncTransportError("sync response was not JSON", status_code=response.status_code) from exc
        if not isinstance(body, dict):
            raise SyncTransportError("sync response envelope was not an object", status_code=response.status_code)
        if body.get("ok") is True:
            data = body.get("data")
            return data if isinstance(data, dict) else {}
        error = body.get("error") or {}
        code = str(error.get("code") or "sync_error") if isinstance(error, dict) else "sync_error"
        message = str(error.get("message") or "sync request failed") if isinstance(error, dict) else "sync error"
        if response.status_code == httpx.codes.FORBIDDEN and code == "consent_required":
            raise SyncConsentRequired(message)
        raise SyncTransportError(message, code=code, status_code=response.status_code)

    async def pull(self, *, since: int, surface: str | None, limit: int, cursor: str | None) -> PullResponse:
        params: dict[str, Any] = {"since": int(since), "limit": int(limit)}
        if surface:
            params["surface"] = surface
        if cursor:
            params["cursor"] = cursor
        try:
            response = await self._client.get("/v1/sync/pull", params=params, headers=await self._headers())
        except httpx.HTTPError as exc:
            raise SyncTransportError("pull transport error: %s" % exc) from exc
        data = self._unwrap(response)
        return PullResponse(
            rows=list(data.get("rows") or []),
            next_cursor=data.get("next_cursor"),
            max_commit_seq=int(data.get("max_commit_seq") or since),
        )

    async def push(self, *, device_id: str, mutations: list[dict[str, Any]]) -> PushResponse:
        payload = {"device_id": device_id, "mutations": mutations}
        try:
            response = await self._client.post("/v1/sync/push", json=payload, headers=await self._headers())
        except httpx.HTTPError as exc:
            raise SyncTransportError("push transport error: %s" % exc) from exc
        data = self._unwrap(response)
        return PushResponse(
            applied=list(data.get("applied") or []),
            conflicts=list(data.get("conflicts") or []),
            rejected=list(data.get("rejected") or []),
        )

    async def put_user_preference(self, *, key: str, value: Any) -> dict[str, Any]:
        """Write one Tier-2 ``user_preferences`` row and return the stored row.

        This is the single-row sibling of :meth:`push`, for a preference the
        desktop must set *now* rather than queue in the outbox — the cloud-sync
        consent toggle being the one that has to be immediate, because the row it
        writes is what every cloud Tier-2 gate reads. The server exempts that key
        from its own consent gate (``SETTINGS_CONSENT_OPTIONAL``) so consent can
        be granted in the first place, and a revoking write additionally runs the
        withdrawal purge inside the same transaction.
        """
        safe_key = quote(str(key).strip(), safe="")
        if not safe_key:
            raise SyncTransportError("user preference key must be non-empty")
        path = USER_PREFERENCE_ROUTE_TEMPLATE.format(key=safe_key)
        try:
            response = await self._client.put(
                path,
                json={"value_json": value},
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            raise SyncTransportError("user-preference transport error: %s" % exc) from exc
        return self._unwrap(response)


__all__ = ["USER_PREFERENCE_ROUTE_TEMPLATE", "HttpSyncTransport"]
