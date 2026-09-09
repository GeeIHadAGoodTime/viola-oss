"""Trace-v2 key provider interface."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.logging_config import get_logger
from core.platform import get_data_dir
from core.user_context import is_cloud_surface, is_desktop_local_principal

logger = get_logger(__name__)

_TRACE_ROOT: Final[Path] = get_data_dir() / "traces" / "by_user"
_AUDIT_PATH: Final[Path] = get_data_dir() / "audit" / "trace_decrypts.jsonl"
_HKDF_INFO: Final[bytes] = b"viola.trace.v2"
_SALT_BYTES: Final[int] = 16
_RAW_KEY_BYTES: Final[int] = 32

# Ordered master-key sources a principal's trace key may be derived from.
# ``auth_db`` is the per-user Fernet key the auth database derives (the only
# source on cloud, where every principal has a real auth row). ``local_vault``
# is the hardware-wrapped, per-user local key the desktop already uses for the
# payment vault -- desktop-local key material that never leaves the device.
MASTER_KEY_SOURCE_AUTH_DB: Final[str] = "auth_db"
MASTER_KEY_SOURCE_LOCAL_VAULT: Final[str] = "local_vault"
MASTER_KEY_SOURCE_EXPLICIT: Final[str] = "explicit"
MASTER_KEY_SOURCE_LOCAL_INSTALL: Final[str] = "local_install"


class TraceAuditError(RuntimeError):
    """Raised when a required trace decrypt audit row cannot be persisted."""


class TraceKeyUnavailableError(RuntimeError):
    """Raised when no master-key source can supply this principal's trace key.

    Carries the per-source failure reasons so the caller that disables trace
    writing reports WHY, instead of surfacing whichever provider-internal
    error happened to be raised last (#4793: a signed-in account UUID has no
    row in the desktop's local auth DB, so the auth source raised
    ``ValueError: User <uuid> not found`` and the run wrote no trace at all).
    """

    def __init__(self, user_id_hash: str, failures: list[tuple[str, BaseException]]) -> None:
        """Build the aggregated message from every source that was tried."""
        self.user_id_hash = user_id_hash
        self.failures = list(failures)
        detail = "; ".join("%s: %s: %s" % (source, type(exc).__name__, exc) for source, exc in failures)
        super().__init__(
            "no trace master key source available for user_id_hash=%s (tried %s)"
            % (user_id_hash, detail or "<no sources>")
        )


class KeyProvider:
    """Per-user trace key unwrap/seal and audited decrypt API."""

    def __init__(self, user_id: str, master_key: bytes | None = None) -> None:
        """Create a provider for desktop or cloud trace key sourcing."""
        if not user_id:
            raise ValueError("user_id is required for trace key access")

        self.user_id = user_id
        self.user_id_hash = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        self.trace_key_path = _TRACE_ROOT / self.user_id_hash / "keys" / "trace_key.enc"
        self._master_key: bytes | None = bytes(master_key) if master_key is not None else None
        self._master_fernet_key: bytes | None = None
        self._master_key_source: str | None = MASTER_KEY_SOURCE_EXPLICIT if master_key is not None else None

    def unwrap_trace_key(self) -> bytes:
        """Return the per-user Fernet key for routine writer/reader internals.

        Sync entry point. On the cloud surface (where ``set_main_loop`` has
        captured the FastAPI lifespan loop) this MUST NOT be called from
        inside async code on the main loop thread — the underlying salt
        lookup goes through ``run_async_synchronously`` which raises
        ``RuntimeError("Cannot synchronously wait on the shared asyncio
        worker loop from itself")``. Async callers should use
        :meth:`unwrap_trace_key_async` instead.
        """
        trace_key = self._unwrap_trace_key()
        self._audit_unwrap()
        return trace_key

    async def unwrap_trace_key_async(self) -> bytes:
        """Async-native variant of :meth:`unwrap_trace_key`.

        Awaits the per-user salt lookup directly on the calling event loop
        instead of dispatching through ``run_async_synchronously``. Use this
        from async contexts (agent_executor.run, task_trace.append_*, blob
        writes during a request handler) to avoid the ASYNC-1 cross-loop
        recursion-into-self failure on cloud.
        """
        trace_key = await self._unwrap_trace_key_async()
        self._audit_unwrap()
        return trace_key

    def engineer_break_glass_unwrap(self, *, task_id: str, reason: str, actor: str) -> bytes:
        """Return the trace key for an explicit engineer/admin content read.

        Use this path for intentional forensic or support access. Routine trace
        writer/reader internals should continue to call ``unwrap_trace_key``.
        """
        if not task_id:
            raise ValueError("task_id is required for break-glass trace key access")
        if not reason:
            raise ValueError("reason is required for break-glass trace key access")
        if not actor:
            raise ValueError("actor is required for break-glass trace key access")

        trace_key = self._unwrap_trace_key()
        self._audit_break_glass(task_id=task_id, reason=reason, actor=actor)
        return trace_key

    async def engineer_break_glass_unwrap_async(self, *, task_id: str, reason: str, actor: str) -> bytes:
        """Async-native variant of :meth:`engineer_break_glass_unwrap`.

        Same fail-closed break-glass audit contract (writes a required
        ``action="break_glass"`` row carrying ``task_id``/``reason``/``actor``
        before returning the key), but awaits the per-user salt + master-key
        resolution directly on the calling event loop instead of dispatching
        through ``run_async_synchronously``. Use this from engineer/support
        trace tools that decrypt inside ``asyncio.run(...)`` in-container (the
        cloud call-trace readers and phone oracles), where the sync break-glass
        path trips the ASYNC-1 cross-loop recursion-into-self failure on cloud.
        """
        if not task_id:
            raise ValueError("task_id is required for break-glass trace key access")
        if not reason:
            raise ValueError("reason is required for break-glass trace key access")
        if not actor:
            raise ValueError("actor is required for break-glass trace key access")

        trace_key = await self._unwrap_trace_key_async()
        self._audit_break_glass(task_id=task_id, reason=reason, actor=actor)
        return trace_key

    def _unwrap_trace_key(self) -> bytes:
        if self.trace_key_path.exists():
            trace_key = self._read_sealed_trace_key()
        else:
            salt = secrets.token_bytes(_SALT_BYTES)
            trace_key = self._derive_trace_key(salt)
            try:
                self._write_sealed_trace_key(trace_key, salt)
            except FileExistsError:
                # Another process sealed the key first. Treat this as
                # seal-or-load and return the durable value.
                trace_key = self._read_sealed_trace_key()
        return trace_key

    async def _unwrap_trace_key_async(self) -> bytes:
        """Async-native variant of :meth:`_unwrap_trace_key`.

        File IO stays synchronous (it's local + cheap and Python's stdlib
        offers no truly-async file primitive); only the salt + master-key
        resolution paths are awaited natively to avoid the cross-loop
        dispatch in ``_resolve_maybe_awaitable``.
        """
        if self.trace_key_path.exists():
            trace_key = await self._read_sealed_trace_key_async()
        else:
            salt = secrets.token_bytes(_SALT_BYTES)
            trace_key = await self._derive_trace_key_async(salt)
            try:
                await self._write_sealed_trace_key_async(trace_key, salt)
            except FileExistsError:
                trace_key = await self._read_sealed_trace_key_async()
        return trace_key

    async def _read_sealed_trace_key_async(self) -> bytes:
        encrypted_key = self._read_sealed_payload()
        try:
            trace_key = Fernet(await self._get_master_fernet_key_async()).decrypt(encrypted_key)
        except InvalidToken as exc:
            trace_key = self._decrypt_sealed_with_alternate_source(encrypted_key, exc)
        return self._coerce_fernet_key(trace_key, key_name="trace_key")

    async def _write_sealed_trace_key_async(self, trace_key: bytes, salt: bytes) -> None:
        if len(salt) != _SALT_BYTES:
            raise ValueError("trace key salt must be %d bytes" % _SALT_BYTES)
        if self.trace_key_path.exists():
            raise FileExistsError(str(self.trace_key_path))
        self.trace_key_path.parent.mkdir(parents=True, exist_ok=True)
        encrypted_key = Fernet(await self._get_master_fernet_key_async()).encrypt(trace_key)
        payload = salt + encrypted_key
        tmp_path = self.trace_key_path.with_name(".%s.%s.tmp" % (self.trace_key_path.name, secrets.token_hex(8)))
        try:
            with tmp_path.open("xb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if self.trace_key_path.exists():
                raise FileExistsError(str(self.trace_key_path))
            os.replace(tmp_path, self.trace_key_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    async def _get_master_fernet_key_async(self) -> bytes:
        if self._master_fernet_key is None:
            self._master_fernet_key = self._coerce_fernet_key(
                await self._get_master_key_material_async(),
                key_name="master_key",
            )
        return self._master_fernet_key

    def seal_trace_key(self, key: bytes) -> None:
        """Seal and persist a per-user trace key."""
        trace_key = self._coerce_fernet_key(key, key_name="trace_key")
        self._write_sealed_trace_key(trace_key, secrets.token_bytes(_SALT_BYTES))

    def audit_decrypt(self, *, task_id: str, reason: str, actor: str) -> None:
        """Record an audited decrypt access for an engineer or support actor."""
        if not task_id:
            raise ValueError("task_id is required for trace decrypt audit")
        if not reason:
            raise ValueError("reason is required for trace decrypt audit")
        if not actor:
            raise ValueError("actor is required for trace decrypt audit")
        self._append_audit_row(
            {
                "ts": _utcnow_iso(),
                "user_id_hash": self.user_id_hash,
                "task_id": task_id,
                "actor": actor,
                "reason": reason,
                "action": "decrypt",
            },
            required=True,
        )

    def _read_sealed_trace_key(self) -> bytes:
        encrypted_key = self._read_sealed_payload()
        try:
            trace_key = Fernet(self._get_master_fernet_key()).decrypt(encrypted_key)
        except InvalidToken as exc:
            trace_key = self._decrypt_sealed_with_alternate_source(encrypted_key, exc)
        return self._coerce_fernet_key(trace_key, key_name="trace_key")

    def _read_sealed_payload(self) -> bytes:
        payload = self.trace_key_path.read_bytes()
        if len(payload) <= _SALT_BYTES:
            raise ValueError("sealed trace key payload is truncated")
        return payload[_SALT_BYTES:]

    def _decrypt_sealed_with_alternate_source(self, encrypted_key: bytes, primary_error: InvalidToken) -> bytes:
        """Decrypt an already-sealed trace key with a non-primary master source.

        A principal's sealed key stays readable when the ORDER of available
        master-key sources changes underneath it -- e.g. an install that sealed
        from the local wrapped-vault key later gains an auth-DB row, which
        would otherwise make every historical trace permanently unreadable
        while looking like key corruption. Without this, the same class that
        produced #4793 (a key source silently unavailable for a principal)
        comes back as silent data loss instead of a silent write skip.
        """
        failures: list[tuple[str, BaseException]] = [(self._master_key_source or "primary", primary_error)]
        for source in self.master_key_sources():
            if source == self._master_key_source:
                continue
            try:
                candidate = self._coerce_fernet_key(
                    self._resolve_master_key_from_source(source),
                    key_name="master_key",
                )
                trace_key = Fernet(candidate).decrypt(encrypted_key)
            except Exception as exc:  # noqa: BLE001, RUF100 - every source failure is reported together
                failures.append((source, exc))
                continue
            logger.warning(
                "Sealed trace key decrypted with alternate master key source %s",
                source,
                extra={
                    "event": "trace_key_master_source_switched",
                    "user_id_hash": self.user_id_hash,
                    "master_key_source": source,
                },
            )
            self._master_key = candidate
            self._master_fernet_key = candidate
            self._master_key_source = source
            return trace_key
        raise TraceKeyUnavailableError(self.user_id_hash, failures)

    def _write_sealed_trace_key(self, trace_key: bytes, salt: bytes) -> None:
        if len(salt) != _SALT_BYTES:
            raise ValueError("trace key salt must be %d bytes" % _SALT_BYTES)
        if self.trace_key_path.exists():
            raise FileExistsError(str(self.trace_key_path))

        self.trace_key_path.parent.mkdir(parents=True, exist_ok=True)
        encrypted_key = Fernet(self._get_master_fernet_key()).encrypt(trace_key)
        payload = salt + encrypted_key
        tmp_path = self.trace_key_path.with_name(".%s.%s.tmp" % (self.trace_key_path.name, secrets.token_hex(8)))

        try:
            with tmp_path.open("xb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if self.trace_key_path.exists():
                raise FileExistsError(str(self.trace_key_path))
            os.replace(tmp_path, self.trace_key_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _derive_trace_key(self, salt: bytes) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_RAW_KEY_BYTES,
            salt=salt,
            info=_HKDF_INFO,
        )
        raw_key = hkdf.derive(self._get_master_key_material())
        # Python bytes are immutable, so HKDF/Fernet key material cannot be
        # reliably zeroized after use. Keep the scope small and persist only the
        # Fernet-sealed key blob.
        return base64.urlsafe_b64encode(raw_key)

    async def _derive_trace_key_async(self, salt: bytes) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_RAW_KEY_BYTES,
            salt=salt,
            info=_HKDF_INFO,
        )
        raw_key = hkdf.derive(await self._get_master_key_material_async())
        return base64.urlsafe_b64encode(raw_key)

    def _get_master_key_material(self) -> bytes:
        if self._master_key is None:
            self._master_key = self._resolve_master_key()
        return self._master_key

    async def _get_master_key_material_async(self) -> bytes:
        if self._master_key is None:
            self._master_key = await self._resolve_master_key_async()
        return self._master_key

    def _get_master_fernet_key(self) -> bytes:
        if self._master_fernet_key is None:
            self._master_fernet_key = self._coerce_fernet_key(
                self._get_master_key_material(),
                key_name="master_key",
            )
        return self._master_fernet_key

    def master_key_sources(self) -> tuple[str, ...]:
        """Return the ordered master-key sources permitted for this principal.

        The auth database is always tried first, so an install that already
        sealed its trace key from the auth-derived master keeps decrypting it.

        The local wrapped-vault key is permitted for any principal on the
        DESKTOP surface, not only for desktop-local pseudo-identities. Traces
        are Tier 3 (desktop-only, never cloud), so a signed-in account's trace
        key is legitimately local key material -- and a GoTrue account has no
        row in the desktop's local auth DB by design (identity is GoTrue's),
        so the auth source can never serve it. Before #4793 the fallback was
        gated on ``is_desktop_local_principal``, which accepts only the legacy
        sentinel and the ``device-`` / ``session:`` / ``transient:`` prefixes,
        so every agent turn run while signed into a cloud account resolved no
        key and silently wrote no trace at all.

        On the CLOUD surface the local-vault source stays restricted to the
        desktop-local pseudo-identities it already covered: a cloud principal
        has a real auth row, a container has no per-user OS keystore, and
        widening it there would invent local key material for a tenant.
        """
        if self._master_key_source == MASTER_KEY_SOURCE_EXPLICIT:
            return (MASTER_KEY_SOURCE_EXPLICIT,)
        sources = [MASTER_KEY_SOURCE_AUTH_DB]
        if self._local_vault_master_key_allowed():
            sources.append(MASTER_KEY_SOURCE_LOCAL_VAULT)
        if not is_cloud_surface():
            sources.append(MASTER_KEY_SOURCE_LOCAL_INSTALL)
        return tuple(sources)

    def _local_vault_master_key_allowed(self) -> bool:
        if is_desktop_local_principal(self.user_id):
            return True
        # ``is_cloud_surface`` fails closed (unknown surface answers cloud), so
        # an unresolvable surface never invents local key material.
        return not is_cloud_surface()

    def _resolve_master_key(self) -> bytes:
        failures: list[tuple[str, BaseException]] = []
        for source in self.master_key_sources():
            try:
                key = self._resolve_master_key_from_source(source)
            except Exception as exc:  # noqa: BLE001, RUF100 - every source failure is reported together
                failures.append((source, exc))
                self._log_master_key_source_failure(source, exc)
                continue
            self._master_key_source = source
            return key
        raise TraceKeyUnavailableError(self.user_id_hash, failures)

    async def _resolve_master_key_async(self) -> bytes:
        failures: list[tuple[str, BaseException]] = []
        for source in self.master_key_sources():
            try:
                key = await self._resolve_master_key_from_source_async(source)
            except Exception as exc:  # noqa: BLE001, RUF100 - every source failure is reported together
                failures.append((source, exc))
                self._log_master_key_source_failure(source, exc)
                continue
            self._master_key_source = source
            return key
        raise TraceKeyUnavailableError(self.user_id_hash, failures)

    def _resolve_master_key_from_source(self, source: str) -> bytes:
        if source == MASTER_KEY_SOURCE_AUTH_DB:
            return self._resolve_auth_master_key()
        if source == MASTER_KEY_SOURCE_LOCAL_VAULT:
            return self._resolve_payment_vault_master_key()
        if source == MASTER_KEY_SOURCE_LOCAL_INSTALL:
            return self._resolve_local_install_master_key()
        raise RuntimeError("unknown trace master key source %r" % source)

    async def _resolve_master_key_from_source_async(self, source: str) -> bytes:
        if source == MASTER_KEY_SOURCE_AUTH_DB:
            return await self._resolve_auth_master_key_async()
        if source == MASTER_KEY_SOURCE_LOCAL_VAULT:
            return self._resolve_payment_vault_master_key()
        if source == MASTER_KEY_SOURCE_LOCAL_INSTALL:
            return self._resolve_local_install_master_key()
        raise RuntimeError("unknown trace master key source %r" % source)

    def _log_master_key_source_failure(self, source: str, exc: BaseException) -> None:
        """Record one source miss at DEBUG -- a miss with a survivor is normal.

        On desktop the auth-DB source misses on EVERY resolve for EVERY
        principal (a GoTrue account has no local auth row, and neither does the
        bootstrap device identity), so logging each miss louder than DEBUG would
        emit a warning per agent turn on every install, forever. That is the
        noise that teaches people to ignore trace-key warnings, which is how
        #4793 stayed invisible in the first place. The loud signal belongs at
        the point where NO source survives: that raises
        ``TraceKeyUnavailableError`` carrying every source and reason, and the
        caller that disables the writer logs it at ERROR.
        """
        logger.debug(
            "Trace master key source %s unavailable; trying the next source: %s",
            source,
            exc,
            extra={
                "event": "trace_key_master_source_failed",
                "user_id_hash": self.user_id_hash,
                "master_key_source": source,
            },
        )

    def _resolve_auth_master_key(self) -> bytes:
        from auth.database import get_auth_db

        db = get_auth_db()
        repo = getattr(db, "oauth_tokens", None)
        if repo is None:
            raise RuntimeError("auth database does not expose oauth_tokens")

        get_user_fernet_key = getattr(repo, "_get_user_fernet_key", None)
        if get_user_fernet_key is None:
            raise RuntimeError("oauth token repository does not expose per-user key derivation")

        parameter_count = len(inspect.signature(get_user_fernet_key).parameters)
        if parameter_count == 1:
            return self._coerce_fernet_key(
                get_user_fernet_key(self.user_id),
                key_name="master_key",
            )
        if parameter_count == 2:
            get_salt = getattr(repo, "_get_or_create_user_salt", None)
            if get_salt is None:
                raise RuntimeError("oauth token repository does not expose per-user salt lookup")
            salt = self._resolve_maybe_awaitable(get_salt(self.user_id))
            return self._coerce_fernet_key(
                get_user_fernet_key(self.user_id, salt),
                key_name="master_key",
            )

        raise RuntimeError("unsupported per-user key derivation signature")

    async def _resolve_auth_master_key_async(self) -> bytes:
        from auth.database import get_auth_db

        db = get_auth_db()
        repo = getattr(db, "oauth_tokens", None)
        if repo is None:
            raise RuntimeError("auth database does not expose oauth_tokens")

        get_user_fernet_key = getattr(repo, "_get_user_fernet_key", None)
        if get_user_fernet_key is None:
            raise RuntimeError("oauth token repository does not expose per-user key derivation")

        parameter_count = len(inspect.signature(get_user_fernet_key).parameters)
        if parameter_count == 1:
            return self._coerce_fernet_key(
                get_user_fernet_key(self.user_id),
                key_name="master_key",
            )
        if parameter_count == 2:
            get_salt = getattr(repo, "_get_or_create_user_salt", None)
            if get_salt is None:
                raise RuntimeError("oauth token repository does not expose per-user salt lookup")
            # Native await — no run_async_synchronously round-trip, so this
            # works on the cloud's main loop without tripping the cross-loop
            # guard in core.asyncio_safe._run_on_worker_loop (which raises
            # RuntimeError on recursion-into-self).
            raw = get_salt(self.user_id)
            salt = await raw if inspect.isawaitable(raw) else raw
            return self._coerce_fernet_key(
                get_user_fernet_key(self.user_id, salt),
                key_name="master_key",
            )

        raise RuntimeError("unsupported per-user key derivation signature")

    def _resolve_local_install_master_key(self) -> bytes:
        """Derive a desktop trace master from the existing local keystore.

        Domain and principal separation prevent reuse as a memory-store key.
        Legacy sources remain first so existing sealed traces remain readable;
        alternate-source decryption also handles later account enrollment.
        """
        if is_cloud_surface():
            raise RuntimeError("Local installation keys are unavailable on cloud")
        from services.memory.key_provider import get_or_create_keystore_secret

        secret, _migrated = get_or_create_keystore_secret()
        if not secret:
            raise RuntimeError("No durable local installation secret is available")
        raw_key = HKDF(
            algorithm=hashes.SHA256(),
            length=_RAW_KEY_BYTES,
            salt=None,
            info=b"viola.trace.local-master.v1\0" + self.user_id.encode("utf-8"),
        ).derive(secret.encode("utf-8"))
        return base64.urlsafe_b64encode(raw_key)

    def _resolve_payment_vault_master_key(self) -> bytes:
        from services.payments.key_protection import get_default_vault_key_protector
        from services.payments.payment_vault import (
            _delete_legacy_keyring_key,
            _get_or_create_key,
        )

        protector = get_default_vault_key_protector(
            _get_or_create_key,
            legacy_key_deleter=_delete_legacy_keyring_key,
        )
        key = protector.unwrap_for_decrypt(user_id=self.user_id, purpose="trace_key")
        return self._coerce_fernet_key(key, key_name="master_key")

    @staticmethod
    def _resolve_maybe_awaitable(value: Any) -> Any:
        if inspect.isawaitable(value):
            from core.asyncio_safe import run_async_synchronously

            return run_async_synchronously(value)
        return value

    @staticmethod
    def _coerce_fernet_key(key: bytes, *, key_name: str) -> bytes:
        key_bytes = bytes(key)
        candidate = base64.urlsafe_b64encode(key_bytes) if len(key_bytes) == _RAW_KEY_BYTES else key_bytes
        try:
            Fernet(candidate)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be a 32-byte raw key or 44-byte Fernet key" % key_name) from exc
        return candidate

    def _audit_unwrap(self) -> None:
        self._append_audit_row(
            {
                "ts": _utcnow_iso(),
                "user_id_hash": self.user_id_hash,
                "actor": "system",
                "reason": "trace_key_unwrap",
                "action": "unwrap",
            }
        )

    def _audit_break_glass(self, *, task_id: str, reason: str, actor: str) -> None:
        self._append_audit_row(
            {
                "ts": _utcnow_iso(),
                "user_id_hash": self.user_id_hash,
                "task_id": task_id,
                "actor": actor,
                "reason": reason,
                "action": "break_glass",
            },
            required=True,
        )

    def _append_audit_row(self, row: dict[str, Any], *, required: bool = False) -> None:
        try:
            _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

            # O_APPEND is sufficient for these short local JSONL rows. Routine
            # unwrap rows stay best-effort; decrypt/break-glass rows pass
            # required=True so content access cannot proceed without audit proof.
            fd = os.open(_AUDIT_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except Exception as exc:
            logger.warning(
                "Trace decrypt audit write failed: %s",
                exc,
                extra={
                    "event": "trace_decrypt_audit_failed",
                    "user_id_hash": self.user_id_hash,
                    "action": row.get("action"),
                    "audit_path": str(_AUDIT_PATH),
                },
            )
            if required:
                raise TraceAuditError("trace decrypt audit write failed") from exc


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()
