"""
Permission System for Third-Party Plugins

User-granted permissions for plugins to access system resources.

F-021: in addition to the legacy global enum buckets (FILES, NETWORK,
AUDIO, SETTINGS, SYSTEM), this module hosts a scoped rule store that
mirrors Claude's permission model — rules carry a tool name, optional
rule content (file path / domain / shell prefix), an allow/deny/ask
behavior, a destination scope (user/project/session/local), and a
classification. Plugin requests and SDK ``can_use_tool`` both consult
the scoped store first and fall back to the flat enum for legacy
plugins that have not been migrated yet.

The destination scope must remain compatible with R9-B's public
``/permissions`` mode boundaries (F-042). The destinations exposed here
match Claude's user/project/session/local set so the SDK side and the
plugin side share one rule store.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

# Compatibility flag set by viola_qt.py before app.exec(). Runtime permission
# dialogs were removed; permission decisions are deterministic below.
EVENT_LOOP_RUNNING = False


class PermissionDeniedError(Exception):
    """Raised when plugin permission is denied"""

    pass


class Permission(Enum):
    """Plugin permissions"""

    FILES = "files"  # Read/write files
    NETWORK = "network"  # Network access
    AUDIO = "audio"  # Audio input/output
    SETTINGS = "settings"  # Modify settings
    SYSTEM = "system"  # System commands (dangerous)


BOOTSTRAP_TEMPORARY_PERMISSIONS = frozenset(
    {
        Permission.FILES,
        Permission.NETWORK,
        Permission.AUDIO,
        Permission.SETTINGS,
    }
)


# F-021: scoped rule model. Behavior and destination match Claude's
# permission schema (entrypoints/sdk/coreSchemas.ts:252-269, :301-329).
RULE_ALLOW = "allow"
RULE_DENY = "deny"
RULE_ASK = "ask"
VALID_BEHAVIORS: frozenset[str] = frozenset({RULE_ALLOW, RULE_DENY, RULE_ASK})

# Coordinates with R9-B F-042 and Claude's SDK
# PermissionUpdateDestinationSchema. ``session`` is the per-process
# default for ad hoc grants; userSettings/projectSettings/localSettings
# persist; ``cliArg`` and ``managed`` are separate policy surfaces.
DEST_USER = "userSettings"
DEST_PROJECT = "projectSettings"
DEST_SESSION = "session"
DEST_LOCAL = "localSettings"
DEST_CLI_ARG = "cliArg"
DEST_MANAGED = "managed"
_DESTINATION_ALIASES: dict[str, str] = {
    "user": DEST_USER,
    "project": DEST_PROJECT,
    "local": DEST_LOCAL,
    DEST_USER: DEST_USER,
    DEST_PROJECT: DEST_PROJECT,
    DEST_SESSION: DEST_SESSION,
    DEST_LOCAL: DEST_LOCAL,
    DEST_CLI_ARG: DEST_CLI_ARG,
    DEST_MANAGED: DEST_MANAGED,
}
VALID_DESTINATIONS: frozenset[str] = frozenset(
    {DEST_USER, DEST_PROJECT, DEST_SESSION, DEST_LOCAL, DEST_CLI_ARG, DEST_MANAGED}
)
VALID_DECISION_CLASSIFICATIONS: frozenset[str] = frozenset({"user_temporary", "user_permanent", "user_reject"})

PERMISSION_MODE_DEFAULT = "default"
PERMISSION_MODE_ACCEPT_EDITS = "acceptEdits"
PERMISSION_MODE_BYPASS_PERMISSIONS = "bypassPermissions"
PERMISSION_MODE_DONT_ASK = "dontAsk"
PERMISSION_MODE_PLAN = "plan"
PERMISSION_MODES: frozenset[str] = frozenset(
    {
        PERMISSION_MODE_DEFAULT,
        PERMISSION_MODE_ACCEPT_EDITS,
        PERMISSION_MODE_BYPASS_PERMISSIONS,
        PERMISSION_MODE_DONT_ASK,
        PERMISSION_MODE_PLAN,
    }
)


def canonicalize_permission_destination(raw: str) -> str:
    destination = _DESTINATION_ALIASES.get(str(raw or "").strip())
    if destination is None:
        raise ValueError("PermissionRule.destination must be one of %s" % sorted(VALID_DESTINATIONS))
    return destination


@dataclass(frozen=True)
class PermissionRule:
    """One scoped permission rule.

    ``tool_name`` is required. ``rule_content`` narrows the rule to a
    path / domain / shell command prefix (e.g. ``Bash(npm test)``,
    ``Read(/etc/*)``). ``behavior`` is allow / deny / ask. ``destination``
    chooses where the rule persists. ``classification`` is informational
    (Claude's "manual" / "model" / "policy").
    """

    plugin_name: str
    tool_name: str
    behavior: str = RULE_ALLOW
    rule_content: str | None = None
    destination: str = DEST_USER
    classification: str = "manual"
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.behavior not in VALID_BEHAVIORS:
            raise ValueError("PermissionRule.behavior must be one of %s" % sorted(VALID_BEHAVIORS))
        object.__setattr__(self, "destination", canonicalize_permission_destination(self.destination))
        if not self.tool_name:
            raise ValueError("PermissionRule.tool_name is required")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.plugin_name, self.tool_name, self.rule_content or "")

    def matches(self, tool_name: str, rule_content: str | None) -> bool:
        if self.tool_name != tool_name and self.tool_name != "*":
            return False
        if self.rule_content is None:
            return True
        if rule_content is None:
            return False
        return rule_content == self.rule_content or rule_content.startswith(self.rule_content)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_permission_string(raw: str) -> tuple[str, str | None]:
    """Split a Claude-style permission string into ``(tool, content)``.

    Examples:
        ``"Bash(npm test)"`` -> ``("Bash", "npm test")``
        ``"Read(/etc/*)"`` -> ``("Read", "/etc/*")``
        ``"files"`` -> ``("files", None)``
    """
    text = (raw or "").strip()
    if not text:
        return ("", None)
    if "(" in text and text.endswith(")"):
        tool, content = text.split("(", 1)
        return (tool.strip(), content[:-1])
    return (text, None)


@dataclass
class PermissionGrant:
    """Permission grant (user approved)"""

    plugin_name: str
    permission: Permission
    granted: bool
    timestamp: float


@dataclass(frozen=True)
class PermissionDecision:
    """Claude-style typed permission decision payload."""

    behavior: str
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None
    decision_reason: str | None = None
    permission_suggestions: list[dict[str, Any]] = field(default_factory=list)
    interrupt: bool | None = None
    tool_use_id: str | None = None
    decision_classification: str | None = None

    def __post_init__(self) -> None:
        if self.behavior not in {"allow", "deny"}:
            raise ValueError("permission decision behavior must be 'allow' or 'deny'")
        if (
            self.decision_classification is not None
            and self.decision_classification not in VALID_DECISION_CLASSIFICATIONS
        ):
            raise ValueError("decision_classification must be one of %s" % sorted(VALID_DECISION_CLASSIFICATIONS))

    @classmethod
    def allow(
        cls,
        updated_input: dict[str, Any] | None = None,
        *,
        updated_permissions: list[dict[str, Any]] | None = None,
        tool_use_id: str | None = None,
        decision_classification: str | None = None,
    ) -> PermissionDecision:
        return cls(
            behavior="allow",
            updated_input=updated_input,
            updated_permissions=updated_permissions or [],
            tool_use_id=tool_use_id,
            decision_classification=decision_classification,
        )

    @classmethod
    def deny(
        cls,
        message: str | None = None,
        *,
        decision_reason: str | None = None,
        interrupt: bool | None = None,
        tool_use_id: str | None = None,
        decision_classification: str | None = None,
    ) -> PermissionDecision:
        return cls(
            behavior="deny",
            message=message,
            decision_reason=decision_reason,
            interrupt=interrupt,
            tool_use_id=tool_use_id,
            decision_classification=decision_classification,
        )

    def with_tool_use_id(self, tool_use_id: str | None) -> PermissionDecision:
        if self.tool_use_id or not tool_use_id:
            return self
        return PermissionDecision(
            behavior=self.behavior,
            updated_input=self.updated_input,
            updated_permissions=self.updated_permissions,
            message=self.message,
            decision_reason=self.decision_reason,
            permission_suggestions=self.permission_suggestions,
            interrupt=self.interrupt,
            tool_use_id=tool_use_id,
            decision_classification=self.decision_classification,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"behavior": self.behavior}
        if self.updated_input is not None:
            payload["updatedInput"] = self.updated_input
        if self.updated_permissions:
            payload["updatedPermissions"] = self.updated_permissions
        if self.message:
            payload["message"] = self.message
        if self.decision_reason:
            payload["decision_reason"] = self.decision_reason
        if self.permission_suggestions:
            payload["permission_suggestions"] = self.permission_suggestions
        if self.interrupt is not None:
            payload["interrupt"] = self.interrupt
        if self.tool_use_id:
            payload["toolUseID"] = self.tool_use_id
        if self.decision_classification:
            payload["decisionClassification"] = self.decision_classification
        return payload

    def to_control_response(self, request_id: str) -> dict[str, Any]:
        return {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": self.to_payload(),
            },
        }


class PermissionManager:
    """Manage plugin permissions.

    Holds two layers:
      * Legacy flat-enum grants (``self.grants``) for plugins that still
        use the FILES/NETWORK/AUDIO/SETTINGS/SYSTEM buckets.
      * F-021 scoped rules (``self._rules``) for the production plugin
        and SDK paths.
    """

    def __init__(self, config_path: Path | None = None):
        """
        Initialize permission manager

        Args:
            config_path: Path to permission grants storage
        """
        if config_path is None:
            config_path = get_data_dir() / "plugin_permissions.json"
        self.config_path = config_path
        self.grants: dict[str, set[Permission]] = {}
        self._temporary_grants: dict[str, set[Permission]] = {}
        # F-021: scoped rule store. Keyed by (plugin_name, tool_name,
        # rule_content). Persisted alongside the legacy grants file.
        self._rules: dict[tuple[str, str, str], PermissionRule] = {}
        # F-021 / R9-B F-042: current permission mode for this session.
        self._permission_mode: str = PERMISSION_MODE_DEFAULT
        self.load_grants()

    def load_grants(self):
        """Load permission grants from disk"""
        if not self.config_path.exists():
            return

        try:
            with open(self.config_path) as f:
                data = json.load(f)

            # The on-disk shape can be one of:
            #   - legacy: {plugin_name: [perm_value, ...]}
            #   - v2: {"version": 2, "grants": {...}, "rules": [...]}
            if isinstance(data, dict) and "version" in data:
                grants_blob = data.get("grants") or {}
                rules_blob = data.get("rules") or []
            else:
                grants_blob = data
                rules_blob = []

            for plugin_name, perm_names in grants_blob.items():
                self.grants[plugin_name] = set()
                for perm in perm_names:
                    try:
                        self.grants[plugin_name].add(Permission(perm))
                    except ValueError:
                        # Migrated scoped rule that ended up in the legacy bucket
                        tool, content = parse_permission_string(perm)
                        if tool:
                            rule = PermissionRule(
                                plugin_name=plugin_name,
                                tool_name=tool,
                                rule_content=content,
                            )
                            self._rules[rule.key] = rule

            for raw in rules_blob:
                try:
                    rule = PermissionRule(
                        plugin_name=str(raw.get("plugin_name", "")),
                        tool_name=str(raw.get("tool_name", "")),
                        behavior=str(raw.get("behavior", RULE_ALLOW)),
                        rule_content=raw.get("rule_content"),
                        destination=str(raw.get("destination", DEST_USER)),
                        classification=str(raw.get("classification", "manual")),
                        created_at=float(raw.get("created_at", time.time())),
                    )
                    if rule.plugin_name and rule.tool_name:
                        self._rules[rule.key] = rule
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping malformed permission rule %s: %s", raw, exc)

            logger.info(
                "Loaded permissions for %s plugins, %s scoped rules",
                len(self.grants),
                len(self._rules),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to load permissions")

    def save_grants(self):
        """Save permission grants to disk.

        The on-disk file is upgraded to the v2 shape that also carries
        scoped F-021 rules. Session-only rules are NOT persisted.
        """
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            # Only persist user-approved grants. Bootstrap-only grants stay in memory.
            grants: dict[str, list[str]] = {}
            for plugin_name, perms in self.grants.items():
                temporary = self._temporary_grants.get(plugin_name, set())
                persisted = sorted(perm.value for perm in perms if perm not in temporary)
                if persisted:
                    grants[plugin_name] = persisted

            rules: list[dict[str, Any]] = []
            for rule in self._rules.values():
                if rule.destination == DEST_SESSION:
                    continue
                rules.append(rule.to_dict())
            rules.sort(key=lambda r: (r.get("plugin_name", ""), r.get("tool_name", ""), r.get("rule_content") or ""))

            payload = {"version": 2, "grants": grants, "rules": rules}
            with open(self.config_path, "w") as f:
                json.dump(payload, f, indent=2)

            logger.info("Permissions saved")
        except (OSError, TypeError, ValueError):
            logger.exception("Failed to save permissions")

    def request_permission(self, plugin_name: str, permission: Permission) -> bool:
        """
        Resolve a plugin permission request using the launch-product policy.

        Args:
            plugin_name: Name of plugin requesting permission
            permission: Permission being requested

        Returns:
            True if granted, False if denied
        """
        if self.is_granted(plugin_name, permission):
            return True

        logger.info("Requesting %s permission for %s", permission.value, plugin_name)
        granted, persist = self._resolve_permission_request(plugin_name, permission)

        if granted:
            self.grant(plugin_name, permission, persist=persist)

        return granted

    def _resolve_permission_request(self, plugin_name: str, permission: Permission) -> tuple[bool, bool]:
        """Decide whether to grant and persist a permission request."""
        if permission in BOOTSTRAP_TEMPORARY_PERMISSIONS:
            logger.info(
                "Temporarily granting %s to %s for this process",
                permission.value,
                plugin_name,
            )
            return True, False

        logger.warning(
            "Denying unsupported runtime plugin permission %s for %s",
            permission.value,
            plugin_name,
        )
        return False, False

    def is_granted(self, plugin_name: str, permission: Permission) -> bool:
        """
        Check if permission is granted

        Args:
            plugin_name: Name of plugin
            permission: Permission to check

        Returns:
            True if permission is granted
        """
        return permission in self.grants.get(plugin_name, set())

    def grant(self, plugin_name: str, permission: Permission, *, persist: bool = True):
        """
        Grant permission.

        Args:
            plugin_name: Name of plugin
            permission: Permission to grant
            persist: Whether to save the grant to disk
        """
        self.grants.setdefault(plugin_name, set()).add(permission)

        if persist:
            temporary = self._temporary_grants.get(plugin_name)
            if temporary is not None:
                temporary.discard(permission)
                if not temporary:
                    del self._temporary_grants[plugin_name]
            self.save_grants()
            logger.info("Granted %s to %s", permission.value, plugin_name)
            return

        self._temporary_grants.setdefault(plugin_name, set()).add(permission)
        logger.info(
            "Temporarily granted %s to %s for this process",
            permission.value,
            plugin_name,
        )

    def revoke(self, plugin_name: str, permission: Permission):
        """
        Revoke permission

        Args:
            plugin_name: Name of plugin
            permission: Permission to revoke
        """
        if plugin_name not in self.grants:
            return

        self.grants[plugin_name].discard(permission)
        if not self.grants[plugin_name]:
            del self.grants[plugin_name]

        temporary = self._temporary_grants.get(plugin_name)
        if temporary is not None:
            temporary.discard(permission)
            if not temporary:
                del self._temporary_grants[plugin_name]

        self.save_grants()
        logger.info("Revoked %s from %s", permission.value, plugin_name)

    # ------------------------------------------------------------------
    # F-021 scoped rules
    # ------------------------------------------------------------------

    def upsert_rule(
        self,
        plugin_name: str,
        permission_text: str,
        *,
        behavior: str = RULE_ALLOW,
        destination: str = DEST_USER,
        classification: str = "manual",
    ) -> PermissionRule:
        """Insert or update a scoped permission rule from a Claude-style string."""
        tool_name, rule_content = parse_permission_string(permission_text)
        if not tool_name:
            raise ValueError("permission_text must include a tool name")
        rule = PermissionRule(
            plugin_name=plugin_name,
            tool_name=tool_name,
            behavior=behavior,
            rule_content=rule_content,
            destination=destination,
            classification=classification,
        )
        self._rules[rule.key] = rule
        if destination != DEST_SESSION:
            self.save_grants()
        return rule

    def remove_rule(
        self,
        plugin_name: str,
        permission_text: str,
    ) -> bool:
        tool_name, rule_content = parse_permission_string(permission_text)
        key = (plugin_name, tool_name, rule_content or "")
        removed = self._rules.pop(key, None) is not None
        if removed:
            self.save_grants()
        return removed

    def list_rules(self, *, plugin_name: str | None = None) -> list[PermissionRule]:
        rules = list(self._rules.values())
        if plugin_name is not None:
            rules = [r for r in rules if r.plugin_name == plugin_name]
        return rules

    def evaluate_rule(
        self,
        *,
        plugin_name: str | None,
        tool_name: str,
        rule_content: str | None = None,
    ) -> PermissionRule | None:
        """Return the most-specific matching rule, or ``None``."""
        candidates: list[PermissionRule] = []
        for rule in self._rules.values():
            if plugin_name is not None and rule.plugin_name != plugin_name and rule.plugin_name != "*":
                continue
            if rule.matches(tool_name, rule_content):
                candidates.append(rule)
        if not candidates:
            return None
        # Most specific wins: exact rule_content > prefix > wildcard.
        candidates.sort(
            key=lambda r: (
                0 if r.rule_content == rule_content else 1,
                -len(r.rule_content or ""),
                0 if r.tool_name == tool_name else 1,
            )
        )
        return candidates[0]

    # ------------------------------------------------------------------
    # Permission mode (R9-B F-042 coordination)
    # ------------------------------------------------------------------

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    def set_permission_mode(self, mode: str, *, user_id: str | None = None) -> None:
        if mode not in PERMISSION_MODES:
            raise ValueError("permission_mode must be one of %s" % sorted(PERMISSION_MODES))
        self._permission_mode = mode
        try:
            from bootstrap.session_state import get_session_state

            get_session_state(user_id=user_id).set_permission_mode(mode)
        except (ImportError, RuntimeError, AttributeError, ValueError) as exc:
            logger.debug("SessionState permission mode update failed: %s", exc)
        logger.info("Permission mode set to %s", mode)

    # ------------------------------------------------------------------
    # F-025 — SDK can_use_tool handler
    # ------------------------------------------------------------------

    def decide_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        plugin_name: str | None = None,
        rule_content: str | None = None,
        tool_use_id: str | None = None,
    ) -> PermissionDecision:
        """Evaluate a Claude SDK ``can_use_tool`` request.

        Honors the current permission mode first:
          * ``bypassPermissions`` -> allow with the original input.
          * ``plan`` -> deny everything (planning mode is read-only).
          * ``acceptEdits`` / ``default`` -> consult scoped rules then
            fall back to the legacy flat grants.

        The decision shape is the Claude-compatible PermissionDecision
        envelope. ``ask`` rules surface as a deny with
        ``decision_reason="ask"`` and a permission_suggestion the SDK
        host can promote to a real prompt.
        """
        if self._permission_mode == PERMISSION_MODE_BYPASS_PERMISSIONS:
            return PermissionDecision.allow(tool_input, tool_use_id=tool_use_id)

        if self._permission_mode == PERMISSION_MODE_PLAN:
            return PermissionDecision.deny(
                "Plan mode is read-only; %s is not allowed." % tool_name,
                decision_reason="plan_mode",
                tool_use_id=tool_use_id,
            )

        if self._permission_mode == PERMISSION_MODE_DONT_ASK:
            return PermissionDecision.deny(
                "Permission mode dontAsk denies %s without prompting." % tool_name,
                decision_reason="dont_ask",
                tool_use_id=tool_use_id,
            )

        rule = self.evaluate_rule(
            plugin_name=plugin_name,
            tool_name=tool_name,
            rule_content=rule_content,
        )
        if rule is not None:
            if rule.behavior == RULE_ALLOW:
                return PermissionDecision.allow(tool_input, tool_use_id=tool_use_id)
            if rule.behavior == RULE_DENY:
                return PermissionDecision(
                    behavior="deny",
                    message="Tool %s denied by rule" % tool_name,
                    decision_reason="rule_deny",
                    tool_use_id=tool_use_id,
                )
            # ask -> surface as a deny with a permission_suggestion so
            # the SDK host can promote it to a UX prompt. The shape
            # mirrors Claude's permission_suggestions list.
            return PermissionDecision(
                behavior="deny",
                message="Tool %s requires user approval" % tool_name,
                decision_reason="ask",
                tool_use_id=tool_use_id,
                permission_suggestions=[
                    {
                        "tool_name": tool_name,
                        "rule_content": rule.rule_content,
                        "destination": rule.destination,
                        "behavior": rule.behavior,
                    }
                ],
            )

        # F-021 legacy fallback: flat enums.
        legacy_map = {
            "Bash": Permission.SYSTEM,
            "Write": Permission.FILES,
            "Edit": Permission.FILES,
            "Read": Permission.FILES,
        }
        legacy_perm = legacy_map.get(tool_name)
        if legacy_perm and plugin_name and self.is_granted(plugin_name, legacy_perm):
            return PermissionDecision.allow(tool_input, tool_use_id=tool_use_id)

        if self._permission_mode == PERMISSION_MODE_ACCEPT_EDITS and tool_name in {"Write", "Edit"}:
            return PermissionDecision.allow(tool_input, tool_use_id=tool_use_id)

        return PermissionDecision(
            behavior="deny",
            message="Tool %s is not allowed by any rule" % tool_name,
            decision_reason="no_matching_rule",
            tool_use_id=tool_use_id,
            permission_suggestions=[
                {
                    "tool_name": tool_name,
                    "rule_content": rule_content,
                    "destination": DEST_SESSION,
                    "behavior": RULE_ASK,
                }
            ],
        )
