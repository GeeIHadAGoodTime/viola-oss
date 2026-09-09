"""Approval manager for agentic tool execution.

Implements channel-agnostic confirmation gates for tools that require
user approval before execution.  Works through any ``MessageChannel``
implementation (voice, Telegram, Discord, Matrix, Slack, console).

Permission policy lives in ``intent.permissions.policy``. This module is
the UI/voice prompt surface that consumes a ``PermissionDecision`` when the
policy says behavior=ask.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from openai import OpenAIError

from config.settings import settings
from core.logging_config import get_logger
from intent.permissions.policy import PermissionDecision
from intent.tool_types import RiskLevel

if TYPE_CHECKING:
    from messaging.channel import MessageChannel

logger = get_logger(__name__)

_APPROVAL_TIMEOUT = 15.0  # seconds to wait for response

# R4-P1-M (2026-05-30): the prior implementation kept three sets of
# substring-match phrases (_YES_PHRASES / _NO_PHRASES /
# _STRONG_YES_PHRASES) and substring-matched the user's response to
# decide approval. This was the parity-violation surface compass also
# surfaced as G11 (voice users saying "yes please do that" or "no
# never mind" was either mis-classified or didn't satisfy the
# DANGEROUS gate). The model is the right interpreter of natural
# language; the runtime should not substring-classify safety-gated
# words. The new shape uses ``ApprovalManager._classify_response``
# which delegates to a background LLM call and fails CLOSED (any
# error / timeout / unclear response -> deny). Tests monkeypatch the
# classification method.

_ApprovalDecision = Literal["strong_affirm", "affirm", "deny", "unclear"]

_APPROVAL_CLASSIFY_SYSTEM_PROMPT = (
    "You are interpreting a user's response to a yes/no approval prompt for a safety-gated "
    "action. Classify the response into exactly one of four categories.\n\n"
    "- strong_affirm: the user explicitly and deliberately confirms a destructive or "
    'irreversible action with no hedging (e.g. "yes I\'m sure", "yes go ahead and do it", '
    '"approved, do it"). Use this only when the user\'s intent is unambiguous and explicit.\n'
    "- affirm: the user affirms but without explicit destructive-action emphasis "
    '(e.g. "sure", "ok", "yes", "go ahead", "yes please").\n'
    '- deny: the user denies (e.g. "no", "cancel", "stop", "don\'t do that", '
    '"actually never mind", "hold off", "not now").\n'
    '- unclear: cannot tell with high confidence (e.g. "maybe", "hmm", "what does that mean", '
    "off-topic, mixed signals).\n\n"
    'Return strictly the JSON object: {"decision": "<category>"} — no prose, no other keys.'
)
_APPROVAL_CLASSIFY_TIMEOUT_S = 10.0
_APPROVAL_CLASSIFY_MAX_TOKENS = 64
_DEFERRED_CONFIRMATION_TTL_S = 300.0
_CONFIRMATION_ARG_KEYS = frozenset(
    {
        "_approval_confirmed",
        "_approval_confirmation_id",
        "confirmation_id",
        "confirmed",
    }
)
_PHONE_CALL_CONFIRMATION_TEXT_FIELDS = frozenset({"task", "caller_name", "extra_context"})
_PHONE_CALL_CONFIRMATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\uff07": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\uff02": '"',
        "\u2013": "-",
        "\u2014": "-",
    }
)


@dataclass(frozen=True)
class _PendingConfirmation:
    confirmation_id: str
    tool_name: str
    action_description: str
    risk: RiskLevel
    args_fingerprint: str
    tool_args: dict[str, Any]
    created_task_id: str | None
    expires_at: float


class _PendingStore:
    """A pending cross-turn confirmation store with its own lock.

    One instance backs one keying scope: either a single ApprovalManager
    (the instance-local fallback) or one authenticated user in the
    process-wide per-user registry below.
    """

    __slots__ = ("confirmations", "lock")

    def __init__(self) -> None:
        self.confirmations: dict[str, _PendingConfirmation] = {}
        self.lock = threading.Lock()


# GitHub #584 hardening — per-USER pending-approval registry.
#
# The cloud pipeline (and its ApprovalManager holding the pending
# cross-turn confirmation) is cached per ``(user_id, device_id)`` in
# ``services/cloud_intent/dispatch.py``. If a client ever sends a
# different ``device_id`` on turn 2 (the confirm), it lands on a fresh
# per-device pipeline whose ApprovalManager has an EMPTY instance-local
# store, so the turn-1 pending is orphaned and the confirm silently
# re-defers. Keying the pending store by the AUTHENTICATED principal
# alone makes that device-switch orphan structurally impossible: any
# device for the same user resolves the SAME store.
#
# Multi-tenant isolation is preserved BY CONSTRUCTION: the key is the
# request's validated ``current_user_id`` (set by the auth middleware
# from the GoTrue token — never a client-supplied field), so customer B
# resolves customer B's store and can NEVER reach customer A's pending,
# even given A's confirmation_id. This is per-user scoping (RLS/scoping
# canon), not a global broadcast: the registry is a dict keyed by
# user_id, exactly the sanctioned per-user cache shape.
_PENDING_STORE_REGISTRY: dict[str, _PendingStore] = {}
_PENDING_STORE_REGISTRY_LOCK = threading.Lock()


def _pending_store_for_user(user_id: str) -> _PendingStore:
    """Return the process-wide pending store for one authenticated user."""

    with _PENDING_STORE_REGISTRY_LOCK:
        store = _PENDING_STORE_REGISTRY.get(user_id)
        if store is None:
            store = _PendingStore()
            _PENDING_STORE_REGISTRY[user_id] = store
        return store


def should_defer_confirmation_on_empty_response(channel: Any | None) -> bool:
    """Return True when a single-turn channel should defer, not deny, on ``ask() -> None``."""

    return getattr(channel, "defer_confirmation_on_ask_none", False) is True


class ConfirmationDeferred(RuntimeError):
    """Raised when approval must continue through the conversational surface."""

    confirmation_required = True
    deferred = True

    def __init__(
        self,
        *,
        confirmation_id: str,
        action_description: str,
        risk: RiskLevel,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        expires_at: float,
    ) -> None:
        super().__init__("confirmation_required")
        self.confirmation_id = confirmation_id
        self.action_description = action_description
        self.risk = risk
        self.tool_name = tool_name
        self.tool_args = dict(tool_args or {})
        self.expires_at = expires_at

    def to_envelope(self) -> dict[str, Any]:
        """Return the existing confirmation_required tool-result shape."""

        return {
            "confirmation_required": True,
            "deferred": True,
            "confirmation_id": self.confirmation_id,
            "tool_name": self.tool_name,
            "risk": self.risk.value,
            "action_description": self.action_description,
            "expires_in_seconds": max(0, int(self.expires_at - time.monotonic())),
            "tool_args": dict(self.tool_args),
            "confirmation_args": {
                "_approval_confirmed": True,
                "_approval_confirmation_id": self.confirmation_id,
            },
            "message": (
                "Confirmation is required before I can %s. Ask the user to confirm or cancel. "
                "If the user confirms, call the same tool again with _approval_confirmed=true "
                "and _approval_confirmation_id='%s'."
            )
            % (self.action_description, self.confirmation_id),
        }


class ApprovalManager:
    """Manages approval gates for tool execution.

    Uses the provided ``MessageChannel`` (voice, text, console, etc.)
    to ask the user and wait for confirmation.  If no channel is available,
    defers confirmation to the conversational surface.
    """

    def __init__(
        self,
        channel: MessageChannel | None = None,
        *,
        # Legacy kwargs preserved for backward-compat wiring
        voice_pipeline: Any | None = None,
        tts_speaker: Any | None = None,
    ) -> None:
        """
        Args:
            channel: A ``MessageChannel`` to use for approval prompts.
                     If ``None``, falls back to legacy voice args or defers.
            voice_pipeline: (Legacy) Voice pipeline with listen_and_record/transcribe.
            tts_speaker: (Legacy) TTSSpeaker or object with speak() method.
        """
        self._channel = channel
        self._pre_approved_tools: set[str] = set()
        # Opus-G8: ``_pre_approved_tools`` is mutated by the agent loop's
        # plan-approval path AND by ``add_pre_approved`` / ``clear_pre_approved``
        # called from request_approval and from per-tool dispatch.  Both run on
        # the asyncio loop today, but ApprovalManager instances can be shared
        # by per-tenant subagents and by background tasks (see
        # ``intent/agent_executor.py`` callsites 4207, 4711).  Guard mutations
        # with a threading lock so concurrent readers never observe a torn set
        # mid-update, and so cross-loop callers cannot race the policy bridge.
        self._pre_approved_lock: threading.Lock = threading.Lock()
        self._last_plan_approval_state: dict[str, Any] | None = None
        # Instance-local fallback pending store, used only when no
        # authenticated principal is resolvable (non-request/test
        # contexts). In the cloud request path the store is resolved
        # per-user via ``_pending_store`` so a turn-2 confirm from a
        # different device_id finds the turn-1 pending (GitHub #584).
        self._local_pending_store = _PendingStore()

        # Legacy support: if no channel provided but legacy voice args given,
        # construct a VoiceChannel adapter automatically.
        if self._channel is None and (voice_pipeline is not None or tts_speaker is not None):
            try:
                from messaging.channels.voice import VoiceChannel

                vc = VoiceChannel(tts_speaker=tts_speaker, voice_pipeline=voice_pipeline)
                if vc.is_available():
                    self._channel = vc
                    logger.debug("ApprovalManager: auto-wrapped legacy voice args into VoiceChannel")
                else:
                    # Keep TTS-only channel for speaking denial messages
                    self._tts_only = tts_speaker
            except Exception as exc:
                logger.debug("Failed to create VoiceChannel from legacy args: %s", exc)
                self._tts_only = tts_speaker
        else:
            self._tts_only = None

    @property
    def channel(self) -> MessageChannel | None:
        """The active channel (if any)."""
        return self._channel

    def clone_for_subagent(self) -> ApprovalManager:
        """Return an isolated copy that shares the channel but scopes pre-approvals.

        Claude Code's worker tools assemble with their own ``permissionMode``
        (``src/tools/AgentTool/AgentTool.tsx:568-578``) — parent pre-approvals
        do NOT leak into the child. Viola previously shared the singleton
        ``ApprovalManager`` so any auto-approve for a parent tool became
        auto-approve in the child too. This clone keeps the user-facing
        channel (so prompts still route correctly) but starts with an empty
        pre-approval set, mirroring the worker-pool isolation.
        """

        cloned = ApprovalManager(channel=self._channel)
        cloned._tts_only = self._tts_only
        # Intentionally NOT copying ``_pre_approved_tools`` or
        # ``_last_plan_approval_state`` — children re-prompt for risky
        # actions independently; parent approval does not authorize a child.
        return cloned

    @staticmethod
    def _args_fingerprint(tool_args: dict[str, Any] | None) -> str:
        return ApprovalManager._args_fingerprint_for_tool("", tool_args)

    @staticmethod
    def _args_fingerprint_for_tool(tool_name: str, tool_args: dict[str, Any] | None) -> str:
        if not isinstance(tool_args, dict):
            return "{}"
        comparable = ApprovalManager._confirmation_comparable_args(tool_name, tool_args)
        return json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _confirmation_comparable_args(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        comparable = {
            key: ApprovalManager._normalize_confirmation_value(value)
            for key, value in tool_args.items()
            if key not in _CONFIRMATION_ARG_KEYS
        }
        if ApprovalManager._is_phone_call_tool_args(tool_name, comparable) and "call_id" in comparable:
            comparable["call_id"] = ""
            for field in _PHONE_CALL_CONFIRMATION_TEXT_FIELDS:
                value = comparable.get(field)
                if isinstance(value, str):
                    comparable[field] = ApprovalManager._normalize_phone_call_confirmation_text(value)
        return comparable

    @staticmethod
    def _is_phone_call_tool_args(tool_name: str, tool_args: dict[str, Any]) -> bool:
        return str(tool_name or "").strip() == "phone" and str(tool_args.get("action") or "").strip().lower() == "call"

    @staticmethod
    def _maybe_prewarm_phone_voice(tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Start waking the remote phone-voice worker for a pending call (C-302a).

        Keyed on the SAME exact tool/action predicate the confirmation
        normalization uses — an identity check on structured tool args, not any
        inspection of model prose — so it fires exactly when an outbound call is
        awaiting the user's answer and never otherwise.

        Best-effort by construction: non-blocking, idempotent, and it swallows
        everything. A pre-warm that cannot start must never affect whether an
        action gets approved, and the call still proves warmth at its own dial
        gate, so failing here costs latency, never correctness.
        """
        if not ApprovalManager._is_phone_call_tool_args(tool_name, tool_args):
            return False
        try:
            from telephony.remote_voice import prewarm_remote_voice

            return bool(prewarm_remote_voice())
        except Exception as exc:  # noqa: BLE001, RUF100 - never let a pre-warm affect approval
            logger.debug("Phone voice pre-warm on approval request failed (ignored): %s", exc)
            return False

    @staticmethod
    def _apply_tool_arg_confirmation_normalization(tool_name: str, tool_args: dict[str, Any]) -> None:
        if ApprovalManager._is_phone_call_tool_args(tool_name, tool_args) and "call_id" in tool_args:
            tool_args["call_id"] = ""

    @staticmethod
    def _normalize_phone_call_confirmation_text(value: str) -> str:
        """Canonicalize harmless model punctuation drift in phone-call prose."""

        normalized = unicodedata.normalize("NFKC", value).translate(_PHONE_CALL_CONFIRMATION_TRANSLATION)
        return " ".join(normalized.split())

    @staticmethod
    def _normalize_confirmation_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ApprovalManager._normalize_confirmation_value(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            if not value:
                return None
            return [ApprovalManager._normalize_confirmation_value(child) for child in value]
        return value

    @staticmethod
    def _stored_tool_args(tool_args: dict[str, Any] | None) -> dict[str, Any]:
        return ApprovalManager._stored_tool_args_for_tool("", tool_args)

    @staticmethod
    def _stored_tool_args_for_tool(tool_name: str, tool_args: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(tool_args, dict):
            return {}
        comparable = {
            key: value for key, value in ApprovalManager._confirmation_comparable_args(tool_name, tool_args).items()
        }
        return json.loads(json.dumps(comparable, sort_keys=True, default=str))

    def _pending_store(self) -> _PendingStore:
        """Resolve the pending-approval store for the current principal.

        Keyed by the request's validated ``current_user_id`` (auth
        middleware, never a client-supplied field), so a confirmation
        deferred on one device is found when the SAME user confirms from
        another device (GitHub #584 — the device-switch orphan). Cross-
        tenant isolation is structural: user B resolves user B's store
        and can never reach user A's pending, even given A's
        confirmation_id.

        Falls back to the instance-local store ONLY when no authenticated
        principal is resolvable (no request context — desktop voice
        callbacks, background tasks, unit tests). That preserves the
        historical single-manager behavior on those paths, where one
        ApprovalManager instance already handles both turns.
        """

        try:
            from core.user_context import get_current_user_id, user_id_or_none

            user_id = user_id_or_none(get_current_user_id())
        except LookupError:
            user_id = None
        if user_id is None:
            return self._local_pending_store
        return _pending_store_for_user(user_id)

    @staticmethod
    def _prune_pending_confirmations(store: _PendingStore, now: float) -> None:
        expired = [
            confirmation_id for confirmation_id, pending in store.confirmations.items() if pending.expires_at <= now
        ]
        for confirmation_id in expired:
            store.confirmations.pop(confirmation_id, None)

    def defer_confirmation(
        self,
        *,
        action_description: str,
        risk: RiskLevel,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> ConfirmationDeferred:
        """Create a pending approval that the next conversational turn can satisfy."""

        now = time.monotonic()
        tool_name_value = str(tool_name or "")
        args_fingerprint = self._args_fingerprint_for_tool(tool_name_value, tool_args)
        stored_tool_args = self._stored_tool_args_for_tool(tool_name_value, tool_args)
        created_task_id = str(task_id) if task_id else None
        store = self._pending_store()
        with store.lock:
            self._prune_pending_confirmations(store, now)
            for pending in store.confirmations.values():
                if (
                    pending.tool_name == tool_name_value
                    and pending.risk == risk
                    and pending.args_fingerprint == args_fingerprint
                ):
                    return ConfirmationDeferred(
                        confirmation_id=pending.confirmation_id,
                        action_description=pending.action_description,
                        risk=pending.risk,
                        tool_name=pending.tool_name,
                        tool_args=pending.tool_args,
                        expires_at=pending.expires_at,
                    )

        confirmation_id = uuid.uuid4().hex
        pending = _PendingConfirmation(
            confirmation_id=confirmation_id,
            tool_name=tool_name_value,
            action_description=action_description,
            risk=risk,
            args_fingerprint=args_fingerprint,
            tool_args=stored_tool_args,
            created_task_id=created_task_id,
            expires_at=now + _DEFERRED_CONFIRMATION_TTL_S,
        )
        with store.lock:
            store.confirmations[confirmation_id] = pending
        return ConfirmationDeferred(
            confirmation_id=confirmation_id,
            action_description=action_description,
            risk=risk,
            tool_name=str(tool_name or ""),
            tool_args=pending.tool_args,
            expires_at=pending.expires_at,
        )

    def pending_confirmations_for_prompt(self) -> list[dict[str, Any]]:
        """Return live pending confirmations for runtime prompt context."""

        now = time.monotonic()
        store = self._pending_store()
        with store.lock:
            self._prune_pending_confirmations(store, now)
            pending_items = list(store.confirmations.values())

        frames: list[dict[str, Any]] = []
        for pending in sorted(pending_items, key=lambda item: item.expires_at):
            frames.append(
                {
                    "confirmation_required": True,
                    "deferred": True,
                    "confirmation_id": pending.confirmation_id,
                    "tool_name": pending.tool_name,
                    "risk": pending.risk.value,
                    "action_description": pending.action_description,
                    "expires_in_seconds": max(0, int(pending.expires_at - now)),
                    "tool_args": dict(pending.tool_args),
                    "confirmation_args": {
                        "_approval_confirmed": True,
                        "_approval_confirmation_id": pending.confirmation_id,
                    },
                    "created_task_id": pending.created_task_id,
                }
            )
        return frames

    def consume_deferred_confirmation(
        self,
        *,
        tool_name: str,
        risk: RiskLevel,
        tool_args: dict[str, Any],
        task_id: str | None = None,
        allow_implicit_match: bool = False,
    ) -> Literal["missing", "accepted", "rejected"]:
        """Validate and consume a structured cross-turn confirmation."""

        if not isinstance(tool_args, dict) or not any(key in tool_args for key in _CONFIRMATION_ARG_KEYS):
            if allow_implicit_match:
                return self._consume_matching_deferred_confirmation(
                    tool_name=tool_name,
                    risk=risk,
                    tool_args=tool_args,
                    task_id=task_id,
                )
            return "missing"

        confirmation_id = str(
            tool_args.get("_approval_confirmation_id") or tool_args.get("confirmation_id") or ""
        ).strip()
        confirmed = tool_args.get("_approval_confirmed", tool_args.get("confirmed"))
        if confirmation_id == "" or confirmed is not True:
            return "rejected"

        now = time.monotonic()
        store = self._pending_store()
        with store.lock:
            self._prune_pending_confirmations(store, now)
            pending = store.confirmations.get(confirmation_id)
            if pending is None:
                return "rejected"
            current_task_id = str(task_id) if task_id else None
            if pending.created_task_id and pending.created_task_id == current_task_id:
                return "rejected"
            if (
                pending.tool_name != str(tool_name or "")
                or pending.risk != risk
                or pending.args_fingerprint != self._args_fingerprint_for_tool(tool_name, tool_args)
            ):
                return "rejected"
            store.confirmations.pop(confirmation_id, None)

        for key in _CONFIRMATION_ARG_KEYS:
            tool_args.pop(key, None)
        self._apply_tool_arg_confirmation_normalization(tool_name, tool_args)
        return "accepted"

    def _consume_matching_deferred_confirmation(
        self,
        *,
        tool_name: str,
        risk: RiskLevel,
        tool_args: dict[str, Any],
        task_id: str | None = None,
    ) -> Literal["missing", "accepted"]:
        """Consume a pending confirmation when the later tool call exactly matches.

        Native tool schemas do not expose Viola's private ``_approval_*`` fields, so
        a confirmed follow-up can only reissue the original tool call. The approval
        remains exact-args and cross-task only; mismatched calls still fall through
        to the normal confirmation gate.
        """

        if not isinstance(tool_args, dict):
            return "missing"

        now = time.monotonic()
        tool_name_value = str(tool_name or "")
        args_fingerprint = self._args_fingerprint_for_tool(tool_name_value, tool_args)
        current_task_id = str(task_id) if task_id else None
        matching_ids: list[str] = []
        store = self._pending_store()
        with store.lock:
            self._prune_pending_confirmations(store, now)
            for confirmation_id, pending in store.confirmations.items():
                if pending.created_task_id and pending.created_task_id == current_task_id:
                    continue
                if (
                    pending.tool_name == tool_name_value
                    and pending.risk == risk
                    and pending.args_fingerprint == args_fingerprint
                ):
                    matching_ids.append(confirmation_id)
            if not matching_ids:
                return "missing"
            for confirmation_id in matching_ids:
                store.confirmations.pop(confirmation_id, None)
        self._apply_tool_arg_confirmation_normalization(tool_name_value, tool_args)
        return "accepted"

    async def request_approval(
        self,
        action_description: str,
        risk: RiskLevel,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> bool:
        """Request user approval for a tool action.

        Args:
            action_description: Human-readable description of the action.
            risk: Risk level of the action.
            tool_name: Optional tool name for pre-approval checking.

        Returns:
            True if approved, False if denied.
        """
        # C-302a: an outbound call is now LIKELY, so start waking the remote
        # voice worker while the user is still confirming. The confirmation turn
        # (Viola asking, the user answering, the confirming agent turn) is dead
        # time today, and the worker's cold start is ~16-37s — so paying it here
        # is the difference between a dial gate that opens immediately and one
        # that stalls the call ~25s before the phone even rings. Non-blocking,
        # idempotent, and a no-op when the remote path is not configured. It
        # never approves anything and never changes the approval outcome.
        self._maybe_prewarm_phone_voice(tool_name, tool_args or {})

        if risk == RiskLevel.SAFE:
            return True

        # Test mode auto-approval bypass — only honored when test_mode is active
        if os.environ.get("VIOLA_TEST_APPROVE_ALL") == "1":
            if settings.test_mode:
                logger.warning(
                    "APPROVAL_BYPASS: test bypass fired for risk=%s action=%s" " — NEVER enable in production",
                    risk.value,
                    action_description,
                )
                return True
            else:
                logger.critical(
                    "APPROVAL_BYPASS: VIOLA_TEST_APPROVE_ALL is set but test_mode is disabled" " — ignoring for safety"
                )
                # Fall through to normal approval flow

        if tool_name:
            with self._pre_approved_lock:
                is_pre_approved = tool_name in self._pre_approved_tools
            if is_pre_approved:
                # H7 invariant: DANGEROUS tools are NEVER auto-approved, even if pre-approved.
                # Pre-approval only bypasses confirmation for SAFE and CONFIRM risk levels.
                if risk == RiskLevel.DANGEROUS:
                    logger.warning(
                        "H7 safety gate: '%s' is DANGEROUS — ignoring pre-approval, requiring explicit confirmation",
                        tool_name,
                    )
                else:
                    logger.info("Tool '%s' pre-approved (risk=%s)", tool_name, risk.value)
                    return True

        confirmation_state = self.consume_deferred_confirmation(
            tool_name=tool_name,
            risk=risk,
            tool_args=tool_args or {},
            task_id=task_id,
            allow_implicit_match=True,
        )
        if confirmation_state == "accepted":
            logger.info(
                "Deferred confirmation accepted for %s action: %s",
                risk.value,
                action_description,
            )
            return True
        if confirmation_state == "rejected":
            logger.warning(
                "Deferred confirmation rejected for %s action: %s",
                risk.value,
                action_description,
            )
            return False

        # Check if an interactive channel is available
        if self._channel is None:
            # G11 fixed (2026-06-01): this branch defers confirmation when reached from the
            # /v1/command HTTP path, which attaches no MessageChannel
            # (intent/ai_controller.py:480 sets self._default_channel = None).
            # Voice/conversational users get a structured confirmation_required
            # envelope so the model - not a runtime keyword classifier - maps
            # the user's next-turn confirmation/cancellation to structured args.
            logger.info(
                "No channel available - deferring %s action confirmation: %s",
                risk.value,
                action_description,
            )
            await self._speak_fallback(
                "Action '%s' requires confirmation. Please confirm or cancel in the conversation." % action_description
            )
            raise self.defer_confirmation(
                action_description=action_description,
                risk=risk,
                tool_name=tool_name,
                tool_args=tool_args,
                task_id=task_id,
            )

        # INT-06 safety invariant: before asking for DANGEROUS approval,
        # verify RISK_MAP was not swapped/mutated for a tool with a weaker
        # classification. If this invariant breaks, refuse the approval
        # rather than silently proceeding.
        if risk == RiskLevel.DANGEROUS:
            import types as _types

            from mcp_hub.approval_bridge import RISK_MAP as _RISK_MAP_RO

            if not isinstance(_RISK_MAP_RO, _types.MappingProxyType):
                logger.critical(
                    "RISK_MAP is not a MappingProxyType (type=%s) — refusing DANGEROUS approval",
                    type(_RISK_MAP_RO).__name__,
                )
                raise RuntimeError("RISK_MAP was mutated — safety invariant broken")

        # Ask the user through the channel
        if risk == RiskLevel.DANGEROUS:
            prompt = (
                "I'd like to %s. This is a potentially destructive action. "
                'Say "yes I\'m sure" to confirm, or "no" to cancel.' % action_description
            )
            buttons = ["Yes, I'm sure", "No"]
        else:
            prompt = "I'd like to %s. Say yes to confirm, or no to cancel." % action_description
            buttons = ["Yes", "No"]

        # Use interactive buttons when the channel supports them
        if hasattr(self._channel, "supports_buttons") and self._channel.supports_buttons:
            response_text = await self._channel.ask_with_buttons(prompt, buttons, timeout=_APPROVAL_TIMEOUT)
        else:
            response_text = await self._channel.ask(prompt, timeout=_APPROVAL_TIMEOUT)
        if response_text is None:
            if should_defer_confirmation_on_empty_response(self._channel):
                logger.info(
                    "Channel cannot answer inline - deferring %s action confirmation: %s",
                    risk.value,
                    action_description,
                )
                raise self.defer_confirmation(
                    action_description=action_description,
                    risk=risk,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    task_id=task_id,
                )
            await self._channel.send("I didn't get a response. Skipping this action.")
            return False

        # R4-P1-M (2026-05-30): replaced the substring keyword-classifier
        # with a model-driven response classification. The model
        # interprets natural language; the runtime branches on its
        # structured ``strong_affirm``/``affirm``/``deny``/``unclear``
        # verdict. Fails CLOSED — any classification error / timeout
        # collapses to ``unclear`` -> deny. Closes the G11 NL-yes voice
        # parity gap (docs/legal/review_2026_05_22/01_DECISIONS.md:197).
        logger.debug("Approval response: '%s' for risk=%s", response_text, risk.value)
        decision = await self._classify_response(response_text, action_description, risk)

        if risk == RiskLevel.DANGEROUS:
            if decision == "strong_affirm":
                logger.info("DANGEROUS action approved (strong_affirm): %s", action_description)
                return True
            if decision == "affirm":
                # Affirm without explicit destructive-action emphasis — re-prompt for
                # a stronger confirmation, then re-classify that response.
                second = await self._channel.ask("This is a destructive action. Please confirm explicitly to proceed.")
                if second:
                    second_decision = await self._classify_response(second, action_description, risk)
                    if second_decision == "strong_affirm":
                        logger.info(
                            "DANGEROUS action approved on second try: %s",
                            action_description,
                        )
                        return True
                await self._channel.send("Okay, I won't do that.")
                return False
            if decision == "deny":
                logger.info("DANGEROUS action denied by user: %s", action_description)
                await self._channel.send("Okay, I won't do that.")
                return False
            # unclear (including model-classifier failure) → fail-closed
            logger.info(
                "Unclear DANGEROUS approval response '%s' — defaulting to deny",
                response_text,
            )
            await self._channel.send("I didn't understand your response. Skipping this action for safety.")
            return False

        # CONFIRM
        if decision in ("strong_affirm", "affirm"):
            logger.info("Action approved: %s", action_description)
            return True
        if decision == "deny":
            logger.info("Action denied by user: %s", action_description)
            await self._channel.send("Okay, I won't do that.")
            return False
        logger.info("Unclear approval response '%s' — defaulting to deny", response_text)
        await self._channel.send("I didn't understand your response. Skipping this action for safety.")
        return False

    async def request_permission_decision(
        self,
        decision: PermissionDecision,
        *,
        action_description: str,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> bool:
        """Consume a policy decision and prompt only for ``ask`` behavior."""

        if decision.behavior == "allow":
            return True
        if decision.behavior == "deny":
            return False
        return await self.request_approval(
            action_description=action_description,
            risk=_risk_from_decision(decision),
            tool_name=tool_name,
            tool_args=tool_args,
            task_id=task_id,
        )

    async def request_plan_approval(
        self,
        plan: list[dict],
        registry: Any | None = None,
    ) -> set[str]:
        """Request approval for a multi-step plan.

        Presents the plan to the user via the channel and asks for batch approval.
        Returns the set of tool names that are pre-approved for this session.

        DANGEROUS tools are NEVER batch-approved — they always require
        individual confirmation.

        Args:
            plan: List of {"tool": "tool_name", "args": {...}} dicts.
            registry: Ignored (kept for backward compatibility).

        Returns:
            Set of pre-approved tool names (empty if denied).
        """
        if not plan:
            return set()

        # Use the MCP approval bridge RISK_MAP for risk lookups
        try:
            from mcp_hub.approval_bridge import RISK_MAP
        except ImportError:
            RISK_MAP = {}

        # Extract unique tool names and classify by risk
        all_tools: set[str] = set()
        dangerous_tools: set[str] = set()
        approvable_tools: set[str] = set()

        for step in plan:
            tool_name = step.get("tool", "")
            if not tool_name:
                continue
            all_tools.add(tool_name)

            risk = RISK_MAP.get(tool_name)
            if risk == RiskLevel.DANGEROUS:
                dangerous_tools.add(tool_name)
                continue

            approvable_tools.add(tool_name)

        # Remove any dangerous tools from approvable set (safety net)
        approvable_tools -= dangerous_tools

        # Build a summary
        ordinals = [
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
            "seventh",
            "eighth",
            "ninth",
            "tenth",
        ]
        step_descriptions: list[str] = []
        for i, step in enumerate(plan):
            tool_name = step.get("tool", "unknown")
            args = step.get("args", {})
            args_summary = ", ".join("%s %s" % (k, v) for k, v in args.items()) if args else ""
            desc = tool_name.replace("_", " ")
            if args_summary:
                desc = "%s with %s" % (desc, args_summary)
            ordinal = ordinals[i] if i < len(ordinals) else "then"
            step_descriptions.append("%s, %s" % (ordinal, desc))

        summary = "I'd like to do %d things: %s." % (
            len(plan),
            "; ".join(step_descriptions),
        )

        if dangerous_tools:
            dangerous_list = ", ".join(sorted(dangerous_tools)).replace("_", " ")
            summary += " The %s step will need separate confirmation." % dangerous_list

        summary += " Should I go ahead?"

        logger.info(
            "Requesting plan approval for %d steps (%d approvable, %d dangerous)",
            len(plan),
            len(approvable_tools),
            len(dangerous_tools),
        )

        if self._channel is None:
            await self._speak_fallback("Plan requires confirmation, but no interactive channel is available. Skipping.")
            return set()

        response_text = await self._channel.ask(summary)
        if response_text is None:
            await self._channel.send("I didn't get a response. I'll ask before each step.")
            return set()

        logger.debug("Plan approval response: '%s'", response_text)
        # R4-P1-M: model-driven classification (same shape as
        # ``request_approval``). Plan-level approval is always CONFIRM
        # risk (DANGEROUS tools were already excluded above), so both
        # ``strong_affirm`` and ``affirm`` count as approval.
        decision = await self._classify_response(
            response_text, "approve plan with %d steps" % len(plan), RiskLevel.CONFIRM
        )
        if decision in ("strong_affirm", "affirm"):
            logger.info(
                "Plan approved — pre-approving tools: %s",
                sorted(approvable_tools),
            )
            with self._pre_approved_lock:
                # Replace under the lock to keep concurrent readers/writers
                # from observing a torn assignment.  Use a fresh set so the
                # caller's snapshot is not aliased.
                self._pre_approved_tools = set(approvable_tools)
            return approvable_tools
        if decision == "deny":
            logger.info("Plan denied by user")
            await self._channel.send("Okay, I won't proceed with that plan.")
            return set()

        # Unclear (including classifier failure) — fall through to
        # per-step approval rather than the destructive paths above.
        logger.info(
            "Unclear plan approval response '%s' — defaulting to per-step approval",
            response_text,
        )
        self._last_plan_approval_state = {
            "type": "plan_approval_unclear",
            "reason": "unclear_user_response",
            "fallback": "per_step_approval",
            "retryable": False,
        }
        return set()

    # -- agent-scoped pre-approval ----------------------------------------------

    def add_pre_approved(self, tools: set[str] | frozenset[str]) -> None:
        """Add tools to the pre-approved set.

        Used by AgentExecutor to auto-approve browser tools during an
        agent task execution.  The payment gate (LLM-level) handles
        safety — not per-tool approval prompts.
        """
        with self._pre_approved_lock:
            self._pre_approved_tools |= tools

    def clear_pre_approved(self, tools: set[str] | frozenset[str] | None = None) -> None:
        """Remove tools from the pre-approved set.

        If *tools* is None, clears all pre-approvals.
        """
        with self._pre_approved_lock:
            if tools is None:
                self._pre_approved_tools.clear()
            else:
                self._pre_approved_tools -= tools

    def snapshot_pre_approved(self) -> frozenset[str]:
        """Return an immutable snapshot of the current pre-approved set.

        Callers that need to read the set should use this instead of touching
        ``_pre_approved_tools`` directly so the lock is honored.
        """
        with self._pre_approved_lock:
            return frozenset(self._pre_approved_tools)

    # -- internal helpers --------------------------------------------------------

    async def _classify_response(
        self,
        response_text: str,
        action_description: str,
        risk: RiskLevel,
    ) -> _ApprovalDecision:
        """Classify a user's approval response via the background LLM.

        Returns one of ``"strong_affirm"``, ``"affirm"``, ``"deny"``,
        ``"unclear"``. Fails CLOSED — any error, timeout, missing key,
        or malformed response collapses to ``"unclear"`` so the
        caller's safe default (re-prompt or deny) applies.

        Tests may monkeypatch this method directly to bypass the LLM
        call.

        R4-P1-M (2026-05-30): this method replaces the retired
        substring-keyword classifier (three phrase-set constants in
        the prior code). The model interprets natural language; the
        runtime never substring-matches the user's words to decide a
        safety gate. The ratchet
        ``check-no-approval-keyword-classifier`` keeps the retired
        constant names and the classifier loop shape from coming back.
        """
        text = (response_text or "").strip()
        if not text:
            return "unclear"

        try:
            from services.openai_background import run_background_openai_response
        except ImportError as exc:
            logger.debug(
                "Approval classifier: background-LLM module unavailable (fail-closed): %s",
                exc,
            )
            return "unclear"

        user_content = (
            "Action requiring confirmation: %s\n"
            "Risk level: %s\n"
            "User's response: %s\n" % (action_description, risk.value, text)
        )

        try:
            raw = await run_background_openai_response(
                system_prompt=_APPROVAL_CLASSIFY_SYSTEM_PROMPT,
                user_content=user_content,
                max_output_tokens=_APPROVAL_CLASSIFY_MAX_TOKENS,
                user_id="",
                timeout_s=_APPROVAL_CLASSIFY_TIMEOUT_S,
            )
        except (TimeoutError, OpenAIError, RuntimeError, ValueError, TypeError) as exc:
            # Fail-CLOSED safety boundary: SDK / transport / timeout /
            # malformed-response errors all collapse to "unclear" so
            # the caller's safe default (deny / re-prompt) applies.
            logger.info(
                "Approval classifier call failed (fail-closed -> unclear): %s",
                exc,
            )
            return "unclear"

        try:
            payload = json.loads(str(raw or "").strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.info(
                "Approval classifier returned non-JSON (fail-closed -> unclear): %s",
                str(raw)[:200],
            )
            return "unclear"

        decision_raw = ""
        if isinstance(payload, dict):
            decision_raw = str(payload.get("decision") or "").strip().lower()
        if decision_raw in ("strong_affirm", "affirm", "deny", "unclear"):
            return decision_raw  # type: ignore[return-value]
        logger.info(
            "Approval classifier returned unknown decision '%s' (fail-closed -> unclear)",
            decision_raw,
        )
        return "unclear"

    async def _speak_fallback(self, text: str) -> None:
        """Speak through channel if available, else try TTS-only fallback."""
        if self._channel is not None:
            await self._channel.send(text)
            return
        # Legacy TTS-only fallback (when voice pipeline has TTS but no STT)
        tts = getattr(self, "_tts_only", None)
        if tts is None:
            return
        try:
            speak = getattr(tts, "speak", None)
            if callable(speak):
                await speak(text)
        except Exception as exc:
            logger.debug("TTS fallback speak failed: %s", exc)


def _risk_from_decision(decision: PermissionDecision) -> RiskLevel:
    risk = decision.risk_level
    if isinstance(risk, RiskLevel):
        return risk
    if isinstance(risk, str):
        try:
            return RiskLevel(risk)
        except ValueError:
            return RiskLevel.DANGEROUS
    return RiskLevel.DANGEROUS
