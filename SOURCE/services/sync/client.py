"""Desktop Tier-2 sync client — pull + push over ``/v1/sync/*`` (ticket #3533).

This is the desktop-side half of the Tier-2 cloud-sync loop. The server half
(``ui/api/routes/sync_bulk.py`` — ``/v1/sync/push`` and ``/v1/sync/pull``) is
already shipped and enforces the ``consent_generation`` era contract (#2780). This
module is deliberately *symmetric* to that server and reuses the in-repo sync
primitives (``services.sync.{version_vector,hlc,conflict}`` + the Tier-3 boundary)
rather than pulling in an OSS sync client with its own incompatible wire protocol
(buy-in-repo, per the ticket's buy-before-build note).

What it does:

* **Consent-gated, fail-closed.** When the local consent flag is off, ``sync_once``
  performs ZERO network writes and ZERO local applies (see ``_consent_gate``). A
  server ``403 consent_required`` is also treated as a clean no-op — the server is
  the second, authoritative gate; the client's local gate is defence-in-depth.
* **Era tracking + stamping.** The client learns the current ``consent_generation``
  era from what it pulls (the authoritative ``consent_cloud_sync`` user_preferences
  row, plus the max era observed across pulled rows) and stamps that era on EVERY
  pushed data mutation. On a ``consent_generation_stale`` reject it refreshes the era
  from a fresh pull and keeps the mutation queued for retry — never silently dropped.
* **Pull (LWW, server wins).** Reads the stored per-device cursor, pages via
  ``next_cursor``, hands each ``{journal,row}`` to the local cache's apply callback
  (cloud is canonical; server rows overwrite local), and advances the cursor.
* **Push.** Builds each mutation body (``version_vector`` via ``vv_bump`` on the
  device actor, ``lww_hlc`` via ``hlc_now``, era stamp), POSTs, and classifies
  applied / conflict / rejected. A ``server_version_wins`` conflict applies the
  returned ``server_row`` to local (server wins, LWW). Other rejects go to a local
  dead-letter record.
* **Tier-3 guard.** The push builder filters Tier-3 payloads client-side so they
  never leave the device — defence-in-depth on top of the server's rejection.

v1 keeps local storage pluggable behind the ``LocalSyncCache`` protocol with an
``InMemorySyncCache`` reference implementation. Per-surface SQLite cache wiring, a
durable on-disk outbox, and Qt-runtime scheduler integration are follow-on tickets
(see the ticket's "what v1 is NOT" list).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from core.logging_config import get_logger
from services.sync.hlc import hlc_now
from services.sync.version_vector import VersionVector, vv_bump
from services.sync_surfaces.base import (
    Tier3SyncPayloadError,
    is_tier3_sync_key,
    reject_tier3_sync_payload,
)

logger = get_logger(__name__)

# The authoritative current-era source on the wire: the consent toggle row itself
# carries the current consent_generation (see
# tests/integration/sync/test_consent_generation_replay.py
# ::test_pull_exposes_current_era_so_client_can_stamp).
CONSENT_SURFACE = "user_preferences"
CONSENT_KEY = "consent_cloud_sync"

# Whole Tier-3 surfaces that must never leave the device (CLAUDE.md three-tier
# storage rule). Field-level Tier-3 leakage is caught separately by
# ``reject_tier3_sync_payload``; this denylist blocks an entire surface by name so
# a mis-tagged Tier-3 surface can never be pushed even if its rows look Tier-2.
TIER3_SURFACES: frozenset[str] = frozenset(
    {
        "payment_vault",
        "byok_keys",
        "byok_api_keys",
        "oauth_tokens",
        "browser_profiles",
        "traces",
        "agent_audit_logs",
        "wake_word_models",
        "local_calendar",
    }
)

PushKind = Literal["applied", "conflict", "rejected", "stale", "filtered"]


class SyncConsentRequired(Exception):
    """Raised by a transport when the server returns 403 consent_required."""


class SyncTransportError(Exception):
    """Raised by a transport for any non-consent error envelope / HTTP failure."""

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class PullResponse:
    rows: list[dict[str, Any]]
    next_cursor: str | None
    max_commit_seq: int


@dataclass(frozen=True)
class PushResponse:
    applied: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    rejected: list[dict[str, Any]]


@dataclass(frozen=True)
class PendingMutation:
    """A locally-authored Tier-2 change awaiting push.

    ``base_version_vector`` / ``base_hlc`` are the entity's last-known CRDT state
    (typically the values the client last pulled for this entity). The push builder
    bumps them on the device actor so the mutation dominates what it was derived
    from — exactly mirroring how the server stamps in ``services.sync.engine.stamp``.
    """

    surface: str
    op: Literal["upsert", "delete"]
    row: dict[str, Any]
    entity_id: str
    last_mutation_id: str
    base_version_vector: VersionVector = field(default_factory=dict)
    # The HLC stamped at LOCAL EDIT time. Sent verbatim so two devices' concurrent
    # edits are ordered by when they were authored (LWW), not by push arrival order.
    # When None, the push builder mints one via hlc_now(actor, observed=base_hlc).
    lww_hlc: str | None = None
    base_hlc: str | None = None


@dataclass(frozen=True)
class PushOutcome:
    mutation: PendingMutation
    kind: PushKind
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    pulled: int = 0
    pushed: int = 0
    conflicts: int = 0
    rejected: int = 0
    stale_requeued: int = 0
    tier3_filtered: int = 0
    consent_skipped: bool = False
    era: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "pulled": self.pulled,
            "pushed": self.pushed,
            "conflicts": self.conflicts,
            "rejected": self.rejected,
            "stale_requeued": self.stale_requeued,
            "tier3_filtered": self.tier3_filtered,
            "consent_skipped": self.consent_skipped,
            "era": self.era,
        }


@runtime_checkable
class LocalSyncCache(Protocol):
    """The pluggable local-storage seam the client applies to and reads from.

    v1 ships ``InMemorySyncCache`` as the reference implementation. The desktop's
    real per-surface SQLite cache is a follow-on ticket — it only has to satisfy
    this protocol.
    """

    def consent_enabled(self) -> bool:
        """Local cloud-sync consent flag. False => the client no-ops entirely."""
        ...

    def pending(self) -> Sequence[PendingMutation]:
        """Locally-authored mutations awaiting push, in dependency/author order."""
        ...

    def get_cursor(self, surface: str | None) -> int:
        """Last-pulled commit_seq for a surface (or the global stream if None)."""
        ...

    def set_cursor(self, surface: str | None, commit_seq: int) -> None:
        """Persist the advanced cursor. Monotonic (never decreases)."""
        ...

    def apply_pulled(self, journal: Mapping[str, Any], row: dict[str, Any] | None) -> None:
        """Apply a pulled ``{journal,row}`` to local storage (LWW: server wins)."""
        ...

    def mark_pushed(self, mutation: PendingMutation, commit_seq: int | None) -> None:
        """Remove a mutation from the pending set after the server accepted it."""
        ...

    def requeue(self, mutation: PendingMutation) -> None:
        """Keep a mutation queued for a later retry (e.g. after era refresh)."""
        ...

    def record_dead_letter(self, mutation: PendingMutation, reason: str, message: str) -> None:
        """Record a permanently-rejected mutation for later inspection."""
        ...


@runtime_checkable
class SyncTransport(Protocol):
    """The HTTP seam. ``HttpSyncTransport`` is the httpx implementation; tests inject
    an in-process fake that faithfully re-implements the era + LWW + consent contract."""

    async def pull(self, *, since: int, surface: str | None, limit: int, cursor: str | None) -> PullResponse: ...

    async def push(self, *, device_id: str, mutations: list[dict[str, Any]]) -> PushResponse: ...


TokenProvider = Callable[[], Awaitable[str | None]]


def _entity_id_of(surface: str, row: Mapping[str, Any]) -> str:
    for key in ("key", "id", "video_id", "profile_id", "capability_id", "provider_id"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return surface


def mutation_is_tier3(surface: str, row: Mapping[str, Any]) -> bool:
    """True if a mutation must be filtered client-side (never leaves the device).

    A whole-surface Tier-3 denylist match, a Tier-3-shaped key anywhere in the row,
    or a Tier-3 secret value anywhere in the row all disqualify it. Reuses the exact
    server-side boundary check so client and server agree on what "Tier-3" means.
    """
    if str(surface).strip() in TIER3_SURFACES:
        return True
    if is_tier3_sync_key(row.get("key", "")):
        return True
    try:
        reject_tier3_sync_payload(surface, dict(row))
    except Tier3SyncPayloadError:
        return True
    return False


class Tier2SyncClient:
    """Symmetric desktop client for the shipped ``/v1/sync/*`` server."""

    def __init__(
        self,
        *,
        transport: SyncTransport,
        cache: LocalSyncCache,
        device_id: str,
        actor_id: str | None = None,
        surfaces: Sequence[str] | None = None,
        page_limit: int = 500,
    ) -> None:
        self._transport = transport
        self._cache = cache
        self._device_id = str(device_id or "").strip()
        if not self._device_id:
            raise ValueError("device_id must be non-empty")
        # The CRDT actor identity for this device. Defaults to the device_id so a
        # device's mutations bump its own version-vector counter.
        self._actor_id = str(actor_id or self._device_id).strip()
        self._surfaces = tuple(surfaces) if surfaces else (None,)
        self._page_limit = max(1, int(page_limit))
        self._era = 0

    @property
    def era(self) -> int:
        """The current consent_generation era the client will stamp on push."""
        return self._era

    # --- consent gate ---------------------------------------------------------

    def _consent_gate(self) -> bool:
        """Return True iff the client may talk to the cloud. Fail-closed."""
        try:
            return bool(self._cache.consent_enabled())
        except Exception:
            # A cache that cannot answer the consent question must NOT be treated
            # as consenting — fail closed (no network, no apply).
            logger.exception("Local consent read failed; treating as consent-off (fail-closed)")
            return False

    # --- era learning ---------------------------------------------------------

    def _learn_era_from_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Update the tracked era from pulled rows: authoritative consent row wins,
        else the max era observed across all pulled rows (monotonic — never lowers)."""
        best = self._era
        for entry in rows:
            row = entry.get("row") if isinstance(entry, Mapping) else None
            if not isinstance(row, Mapping):
                continue
            era = row.get("consent_generation")
            if era is None:
                continue
            try:
                era_int = int(era)
            except (TypeError, ValueError):
                continue
            journal = entry.get("journal") if isinstance(entry, Mapping) else None
            surface = str((journal or {}).get("surface") or "")
            is_consent_row = surface == CONSENT_SURFACE and str(row.get("key")) == CONSENT_KEY
            if is_consent_row:
                # The consent toggle row is the authoritative current-era source.
                best = max(best, era_int)
            else:
                best = max(best, era_int)
        if best > self._era:
            self._era = best

    async def refresh_era(self) -> int:
        """Re-pull the authoritative consent row to refresh the stamped era.

        Used after a ``consent_generation_stale`` reject so the retry stamps the
        fresh era. Does not advance any data cursor (a since=0 read of the consent
        surface only), so it never re-materialises or skips data rows.
        """
        if not self._consent_gate():
            return self._era
        try:
            response = await self._transport.pull(since=0, surface=CONSENT_SURFACE, limit=self._page_limit, cursor=None)
        except SyncConsentRequired:
            return self._era
        self._learn_era_from_rows(response.rows)
        return self._era

    # --- pull -----------------------------------------------------------------

    async def pull_once(self) -> SyncResult:
        result = SyncResult()
        if not self._consent_gate():
            result.consent_skipped = True
            result.era = self._era
            return result
        for surface in self._surfaces:
            try:
                pulled = await self._pull_surface(surface, result)
            except SyncConsentRequired:
                # Server-side gate says consent is off => clean no-op for the rest.
                result.consent_skipped = True
                break
            result.pulled += pulled
        result.era = self._era
        return result

    async def _pull_surface(self, surface: str | None, result: SyncResult) -> int:
        cursor_seq = self._cache.get_cursor(surface)
        applied = 0
        cursor: str | None = None
        while True:
            response = await self._transport.pull(
                since=cursor_seq, surface=surface, limit=self._page_limit, cursor=cursor
            )
            self._learn_era_from_rows(response.rows)
            for entry in response.rows:
                journal = entry.get("journal") or {}
                row = entry.get("row")
                self._cache.apply_pulled(journal, row if isinstance(row, dict) else None)
                applied += 1
            if response.max_commit_seq > cursor_seq:
                cursor_seq = response.max_commit_seq
                self._cache.set_cursor(surface, cursor_seq)
            if not response.next_cursor:
                break
            cursor = response.next_cursor
        return applied

    # --- push -----------------------------------------------------------------

    def _build_body(self, mutation: PendingMutation) -> dict[str, Any]:
        """Build the on-wire push body for a pending mutation, stamping the era."""
        version_vector = vv_bump(dict(mutation.base_version_vector), self._actor_id)
        lww_hlc = mutation.lww_hlc or hlc_now(self._actor_id, observed=mutation.base_hlc)
        return {
            "surface": mutation.surface,
            "op": mutation.op,
            "row": dict(mutation.row),
            "version_vector": version_vector,
            "lww_hlc": lww_hlc,
            "lww_actor_id": self._actor_id,
            "last_mutation_id": mutation.last_mutation_id,
            "field_versions": {},
            "consent_generation": self._era,
        }

    def _is_consent_optional(self, mutation: PendingMutation) -> bool:
        return mutation.surface == CONSENT_SURFACE and str(mutation.row.get("key", "")) == CONSENT_KEY

    async def push_once(self) -> SyncResult:
        result = SyncResult()
        result.era = self._era
        if not self._consent_gate():
            result.consent_skipped = True
            return result

        pending = list(self._cache.pending())
        if not pending:
            return result

        # Client-side Tier-3 guard: filter Tier-3 payloads before they hit the wire.
        sendable: list[PendingMutation] = []
        for mutation in pending:
            if not self._is_consent_optional(mutation) and mutation_is_tier3(mutation.surface, mutation.row):
                result.tier3_filtered += 1
                self._cache.record_dead_letter(
                    mutation, "tier3_filtered_client_side", "Tier-3 payload never leaves the device."
                )
                self._cache.mark_pushed(mutation, None)
                logger.warning("Filtered Tier-3 mutation on surface=%s before push", mutation.surface)
                continue
            sendable.append(mutation)

        if not sendable:
            return result

        bodies = [self._build_body(mutation) for mutation in sendable]
        index_to_mutation = dict(enumerate(sendable))
        try:
            response = await self._transport.push(device_id=self._device_id, mutations=bodies)
        except SyncConsentRequired:
            # Consent went off between the gate and the write — clean no-op, keep
            # everything queued (nothing was applied server-side).
            result.consent_skipped = True
            return result

        stale_indices: set[int] = set()
        self._classify_push(response, index_to_mutation, result, stale_indices)

        # Era-refresh + retry-once for the stale batch (withdraw -> re-grant recovery).
        if stale_indices:
            await self.refresh_era()
            await self._retry_stale(index_to_mutation, stale_indices, result)

        result.era = self._era
        return result

    def _classify_push(
        self,
        response: PushResponse,
        index_to_mutation: dict[int, PendingMutation],
        result: SyncResult,
        stale_indices: set[int],
    ) -> None:
        for entry in response.applied:
            index = int(entry.get("index", -1))
            mutation = index_to_mutation.get(index)
            if mutation is None:
                continue
            self._cache.mark_pushed(mutation, entry.get("commit_seq"))
            result.pushed += 1
        for entry in response.conflicts:
            index = int(entry.get("index", -1))
            mutation = index_to_mutation.get(index)
            if mutation is None:
                continue
            result.conflicts += 1
            self._apply_conflict(mutation, entry)
        for entry in response.rejected:
            index = int(entry.get("index", -1))
            mutation = index_to_mutation.get(index)
            if mutation is None:
                continue
            reason = str(entry.get("reason") or entry.get("rejected") or "rejected")
            if reason == "consent_generation_stale":
                # Do NOT drop — keep queued; the era refresh + retry handles it.
                stale_indices.add(index)
                result.stale_requeued += 1
                self._cache.requeue(mutation)
                continue
            result.rejected += 1
            self._cache.record_dead_letter(mutation, reason, str(entry.get("details") or reason))
            self._cache.mark_pushed(mutation, None)

    def _apply_conflict(self, mutation: PendingMutation, entry: Mapping[str, Any]) -> None:
        """LWW, server wins: apply the returned server_row to local and clear the
        pending mutation. If the server returned no row (tombstone/delete case), we
        still clear the pending mutation — the server's decision is canonical."""
        server_row = entry.get("server_row")
        if isinstance(server_row, dict):
            synthetic_journal = {
                "surface": mutation.surface,
                "entity_id": _entity_id_of(mutation.surface, server_row),
                "op": "delete" if server_row.get("deleted_at") else "upsert",
                "commit_seq": server_row.get("commit_seq"),
            }
            self._cache.apply_pulled(synthetic_journal, server_row)
        self._cache.mark_pushed(mutation, None)

    async def _retry_stale(
        self,
        index_to_mutation: dict[int, PendingMutation],
        stale_indices: set[int],
        result: SyncResult,
    ) -> None:
        retry = [index_to_mutation[index] for index in sorted(stale_indices)]
        bodies = [self._build_body(mutation) for mutation in retry]
        retry_index_map = dict(enumerate(retry))
        try:
            response = await self._transport.push(device_id=self._device_id, mutations=bodies)
        except SyncConsentRequired:
            result.consent_skipped = True
            return
        for entry in response.applied:
            mutation = retry_index_map.get(int(entry.get("index", -1)))
            if mutation is None:
                continue
            self._cache.mark_pushed(mutation, entry.get("commit_seq"))
            result.pushed += 1
            result.stale_requeued -= 1
        for entry in response.conflicts:
            mutation = retry_index_map.get(int(entry.get("index", -1)))
            if mutation is None:
                continue
            result.conflicts += 1
            result.stale_requeued -= 1
            self._apply_conflict(mutation, entry)
        for entry in response.rejected:
            mutation = retry_index_map.get(int(entry.get("index", -1)))
            if mutation is None:
                continue
            reason = str(entry.get("reason") or "rejected")
            # Still stale after a refresh, or a different reject — keep queued for a
            # later cycle rather than dropping (never silently lose a mutation).
            self._cache.requeue(mutation)

    # --- combined -------------------------------------------------------------

    async def sync_once(self) -> SyncResult:
        """Pull then push once. Pull first so the era + local base state are current
        before we stamp and push."""
        if not self._consent_gate():
            return SyncResult(consent_skipped=True, era=self._era)
        pull_result = await self.pull_once()
        if pull_result.consent_skipped:
            return pull_result
        push_result = await self.push_once()
        return SyncResult(
            pulled=pull_result.pulled,
            pushed=push_result.pushed,
            conflicts=push_result.conflicts,
            rejected=push_result.rejected,
            stale_requeued=push_result.stale_requeued,
            tier3_filtered=push_result.tier3_filtered,
            consent_skipped=push_result.consent_skipped,
            era=self._era,
        )


# ---------------------------------------------------------------------------
# Reference in-memory local cache (v1). Real per-surface SQLite cache is follow-on.
# ---------------------------------------------------------------------------


class InMemorySyncCache:
    """A minimal, correct ``LocalSyncCache`` used as the v1 reference + by tests.

    Rows are keyed ``(surface, entity_id)``. ``apply_pulled`` is LWW-server-wins:
    the pulled/returned server row overwrites local unconditionally (cloud is
    canonical for pulls; on a conflict the client already ran ``resolve`` and the
    server's version won). Cursors are monotonic.
    """

    def __init__(self, *, consent: bool = True) -> None:
        self._consent = bool(consent)
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.cursors: dict[str, int] = {}
        self._pending: list[PendingMutation] = []
        self.dead_letters: list[dict[str, Any]] = []
        self.applied_journal: list[dict[str, Any]] = []

    # consent
    def consent_enabled(self) -> bool:
        return self._consent

    def set_consent(self, value: bool) -> None:
        self._consent = bool(value)

    # outbox
    def queue(self, mutation: PendingMutation) -> None:
        self._pending.append(mutation)

    def pending(self) -> Sequence[PendingMutation]:
        return list(self._pending)

    def mark_pushed(self, mutation: PendingMutation, commit_seq: int | None) -> None:
        self._pending = [item for item in self._pending if item is not mutation]

    def requeue(self, mutation: PendingMutation) -> None:
        # Already retained in _pending unless mark_pushed removed it; ensure present.
        if mutation not in self._pending:
            self._pending.append(mutation)

    def record_dead_letter(self, mutation: PendingMutation, reason: str, message: str) -> None:
        self.dead_letters.append(
            {"surface": mutation.surface, "entity_id": mutation.entity_id, "reason": reason, "message": message}
        )

    # cursors
    def _cursor_key(self, surface: str | None) -> str:
        return surface or "__all__"

    def get_cursor(self, surface: str | None) -> int:
        return int(self.cursors.get(self._cursor_key(surface), 0))

    def set_cursor(self, surface: str | None, commit_seq: int) -> None:
        key = self._cursor_key(surface)
        if int(commit_seq) > self.cursors.get(key, 0):
            self.cursors[key] = int(commit_seq)

    # apply
    def apply_pulled(self, journal: Mapping[str, Any], row: dict[str, Any] | None) -> None:
        surface = str(journal.get("surface") or "")
        entity_id = str(journal.get("entity_id") or (_entity_id_of(surface, row) if row else surface))
        self.applied_journal.append({"surface": surface, "entity_id": entity_id, "op": journal.get("op")})
        deleted = bool((row or {}).get("deleted_at")) or str(journal.get("op")) == "delete"
        if row is None or deleted:
            self.rows.pop((surface, entity_id), None)
            return
        self.rows[(surface, entity_id)] = dict(row)

    def value(self, surface: str, entity_id: str) -> dict[str, Any] | None:
        row = self.rows.get((surface, entity_id))
        return dict(row) if row is not None else None


__all__ = [
    "CONSENT_KEY",
    "CONSENT_SURFACE",
    "TIER3_SURFACES",
    "InMemorySyncCache",
    "LocalSyncCache",
    "PendingMutation",
    "PullResponse",
    "PushOutcome",
    "PushResponse",
    "SyncConsentRequired",
    "SyncResult",
    "SyncTransport",
    "SyncTransportError",
    "Tier2SyncClient",
    "TokenProvider",
    "mutation_is_tier3",
]
