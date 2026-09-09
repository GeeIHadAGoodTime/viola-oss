"""Certification contract for connector compatibility claims.

This module is intentionally deterministic and side-effect free.  It turns the
connector manifests plus current per-user runtime status into an explicit
certification matrix.  Release tooling can then make an honest claim:
"100% of the defined certification contract passed" only when every required
check is pass, not merely because a provider is listed in Settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from services.connectors.manifests import SCHEMA_VERSION, get_connector_manifest, list_connector_manifests
from services.connectors.models import ConnectorManifest, ConnectorStatus
from services.connectors.status import build_connector_statuses

CERTIFICATION_SCHEMA_VERSION = 2
CERTIFICATION_CONTRACT_VERSION = "2026-05-17.release-provider-account-isolation.v2"
SUPPORTED_CATEGORIES = frozenset({"llm", "music", "smart_home"})

_PASS = "pass"
_FAIL = "fail"
_MISSING = "missing"
_SKIP = "skip"


@dataclass(frozen=True, slots=True)
class CertificationCheck:
    """One certification assertion for one connector."""

    id: str
    label: str
    scope: str
    required: bool
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "required": self.required,
            "status": self.status,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


@dataclass(frozen=True, slots=True)
class ConnectorCertification:
    """Certification rollup for one connector."""

    connector_id: str
    category: str
    tier: str
    claim: str
    required_passed: int
    required_total: int
    required_missing: int
    required_failed: int
    checks: tuple[CertificationCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "category": self.category,
            "tier": self.tier,
            "claim": self.claim,
            "required_passed": self.required_passed,
            "required_total": self.required_total,
            "required_missing": self.required_missing,
            "required_failed": self.required_failed,
            "checks": [check.to_dict() for check in self.checks],
        }


def _check(
    check_id: str,
    label: str,
    *,
    scope: str,
    required: bool,
    status: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
    remediation: str = "",
) -> CertificationCheck:
    return CertificationCheck(
        id=check_id,
        label=label,
        scope=scope,
        required=required,
        status=status,
        detail=detail,
        evidence=evidence or {},
        remediation=remediation,
    )


def _required_summary(checks: list[CertificationCheck]) -> tuple[int, int, int, int]:
    required = [check for check in checks if check.required]
    passed = sum(1 for check in required if check.status == _PASS)
    missing = sum(1 for check in required if check.status == _MISSING)
    failed = sum(1 for check in required if check.status == _FAIL)
    return passed, len(required), missing, failed


def _tier_for_checks(checks: list[CertificationCheck]) -> tuple[str, str]:
    required_passed, required_total, required_missing, required_failed = _required_summary(checks)
    if required_failed:
        return (
            "blocked",
            "Required certification checks failed; this connector cannot be called certified.",
        )
    if required_missing:
        return (
            "compatible",
            "Static contract passed, but live proof is missing. This connector is compatible, not certified.",
        )
    if required_total and required_passed == required_total:
        return (
            "certified",
            "All required certification checks passed for this connector in this environment.",
        )
    return (
        "uncertified",
        "No required certification checks were defined for this connector.",
    )


def _known_auth_types() -> set[str]:
    return {
        "api_key",
        "local_path",
        "local_network_scan",
        "local_server",
        "managed",
        "oauth_or_browser_session",
        "subscription",
        "url_token",
    }


def _known_privacy_boundaries() -> set[str]:
    return {"cloud", "local", "local_or_lan", "managed_cloud"}


def _manifest_static_checks(manifest: ConnectorManifest) -> list[CertificationCheck]:
    checks: list[CertificationCheck] = []
    required_fields = {
        "id": manifest.id,
        "category": manifest.category,
        "display_name": manifest.display_name,
        "connector_kind": manifest.connector_kind,
        "provider_key": manifest.provider_key,
        "adapter": manifest.adapter,
        "auth_type": manifest.auth_type,
        "privacy_boundary": manifest.privacy_boundary,
    }
    missing = sorted(field for field, value in required_fields.items() if not str(value or "").strip())
    checks.append(
        _check(
            "manifest.identity",
            "Manifest identity is complete",
            scope="static",
            required=True,
            status=_PASS if not missing else _FAIL,
            detail=(
                "Required manifest identity fields are present."
                if not missing
                else "Manifest identity fields are missing."
            ),
            evidence={"missing": missing},
            remediation="Fill every required ConnectorManifest identity field.",
        )
    )

    category_ok = manifest.category in SUPPORTED_CATEGORIES and manifest.id.startswith("%s." % manifest.category)
    checks.append(
        _check(
            "manifest.category_prefix",
            "Connector ID is category-scoped",
            scope="static",
            required=True,
            status=_PASS if category_ok else _FAIL,
            detail=(
                "Connector ID prefix matches its category."
                if category_ok
                else "Connector ID must be prefixed with its category."
            ),
            evidence={"id": manifest.id, "category": manifest.category},
            remediation="Use IDs like llm.openai, music.spotify, or smart_home.home_assistant.",
        )
    )

    auth_ok = manifest.auth_type in _known_auth_types()
    privacy_ok = manifest.privacy_boundary in _known_privacy_boundaries()
    checks.append(
        _check(
            "manifest.security_boundary",
            "Auth and privacy boundaries are explicit",
            scope="static",
            required=True,
            status=_PASS if auth_ok and privacy_ok else _FAIL,
            detail=(
                "Auth type and privacy boundary are known."
                if auth_ok and privacy_ok
                else "Auth type or privacy boundary is unknown."
            ),
            evidence={"auth_type": manifest.auth_type, "privacy_boundary": manifest.privacy_boundary},
            remediation="Use a known auth_type and privacy_boundary so Settings can explain data flow.",
        )
    )

    action_errors: list[str] = []
    if not manifest.actions:
        action_errors.append("missing_actions")
    action_ids = {action.id for action in manifest.actions}
    for action in manifest.actions:
        if not action.method or not action.path:
            action_errors.append("%s_shape" % action.id)
        if action.id == "select" and (action.mutates_connection or action.destructive):
            action_errors.append("select_mutates_connection")
        if action.id in {"disconnect", "revoke"} and not action.destructive:
            action_errors.append("%s_not_destructive" % action.id)
    checks.append(
        _check(
            "manifest.action_semantics",
            "Actions declare side effects",
            scope="static",
            required=True,
            status=_PASS if not action_errors else _FAIL,
            detail=(
                "Action side effects are explicit and safe."
                if not action_errors
                else "One or more actions have unsafe or incomplete side-effect metadata."
            ),
            evidence={"actions": sorted(action_ids), "errors": action_errors},
            remediation="Keep selection, connection, and destructive actions separate.",
        )
    )
    return checks


def _llm_static_checks(manifest: ConnectorManifest) -> list[CertificationCheck]:
    capabilities = manifest.capabilities
    required_capabilities = ("chat", "agent_routing", "native_tool_contract", "model_discovery", "context_detection")
    missing_caps = [name for name in required_capabilities if name not in capabilities]
    checks = [
        _check(
            "llm.static.capability_contract",
            "LLM capability contract is declared",
            scope="static",
            required=True,
            status=_PASS if not missing_caps else _FAIL,
            detail=(
                "LLM manifest declares chat, agent, tool, model, and context capabilities."
                if not missing_caps
                else "LLM manifest is missing required capability declarations."
            ),
            evidence={"missing": missing_caps},
            remediation="Declare all LLM capability keys, even when provider-dependent.",
        )
    ]
    if manifest.requires_api_key and manifest.auth_type != "api_key":
        checks.append(
            _check(
                "llm.static.api_key_auth_consistency",
                "API-key requirement matches auth type",
                scope="static",
                required=True,
                status=_FAIL,
                detail="Connector requires an API key but does not use api_key auth.",
                evidence={"auth_type": manifest.auth_type},
                remediation="Set auth_type='api_key' or requires_api_key=False.",
            )
        )
    else:
        checks.append(
            _check(
                "llm.static.api_key_auth_consistency",
                "API-key requirement matches auth type",
                scope="static",
                required=True,
                status=_PASS,
                detail="API-key requirement and auth type are coherent.",
            )
        )
    return checks


def _music_static_checks(manifest: ConnectorManifest) -> list[CertificationCheck]:
    action_ids = {action.id for action in manifest.actions}
    select = next((action for action in manifest.actions if action.id == "select"), None)
    select_ok = bool(select and select.mutates_selection and not select.mutates_connection and not select.destructive)
    playback_ok = manifest.capabilities.get("playback") is True
    return [
        _check(
            "music.static.select_is_not_connect",
            "Selection is independent from auth",
            scope="static",
            required=True,
            status=_PASS if select_ok else _FAIL,
            detail=(
                "Music select action changes only selected source."
                if select_ok
                else "Music select action must not mutate connection state."
            ),
            evidence={"actions": sorted(action_ids)},
            remediation="Expose select/connect/disconnect as separate actions.",
        ),
        _check(
            "music.static.playback_capability",
            "Playback capability is declared",
            scope="static",
            required=True,
            status=_PASS if playback_ok else _FAIL,
            detail=(
                "Music connector declares playback support."
                if playback_ok
                else "Music connector must declare playback support or be removed from music sources."
            ),
            evidence={"capabilities": dict(manifest.capabilities)},
        ),
    ]


def _smart_home_static_checks(manifest: ConnectorManifest) -> list[CertificationCheck]:
    capability_ok = bool(manifest.capabilities)
    local_ok = manifest.privacy_boundary in {"local", "local_or_lan"}
    return [
        _check(
            "smart_home.static.capability_contract",
            "Smart-home capability contract is declared",
            scope="static",
            required=True,
            status=_PASS if capability_ok else _FAIL,
            detail=(
                "Smart-home connector declares its capabilities."
                if capability_ok
                else "Smart-home connector has no capabilities."
            ),
            evidence={"capabilities": dict(manifest.capabilities)},
        ),
        _check(
            "smart_home.static.local_boundary",
            "Smart-home boundary is local or LAN",
            scope="static",
            required=True,
            status=_PASS if local_ok else _FAIL,
            detail=(
                "Smart-home connector is scoped to local/LAN data flow."
                if local_ok
                else "Smart-home connectors must not be silently cloud-scoped."
            ),
            evidence={"privacy_boundary": manifest.privacy_boundary},
        ),
    ]


def _category_static_checks(manifest: ConnectorManifest) -> list[CertificationCheck]:
    if manifest.category == "llm":
        return _llm_static_checks(manifest)
    if manifest.category == "music":
        return _music_static_checks(manifest)
    if manifest.category == "smart_home":
        return _smart_home_static_checks(manifest)
    return []


def _status_by_id(statuses: list[ConnectorStatus]) -> dict[str, ConnectorStatus]:
    return {status.id: status for status in statuses}


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _connector_evidence(evidence: dict[str, Any] | None, connector_id: str) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    connectors = evidence.get("connectors")
    if isinstance(connectors, dict) and isinstance(connectors.get(connector_id), dict):
        return connectors[connector_id]
    direct = evidence.get(connector_id)
    return direct if isinstance(direct, dict) else {}


def _release_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    release = evidence.get("release")
    return release if isinstance(release, dict) else {}


def _probe_check(
    check_id: str,
    label: str,
    probe: object,
    *,
    scope: str,
    required: bool,
    pass_detail: str,
    missing_detail: str,
    fail_detail: str,
    remediation: str,
) -> CertificationCheck:
    status, probe_evidence = _probe_result(probe)
    return _check(
        check_id,
        label,
        scope=scope,
        required=required,
        status=status,
        detail=pass_detail if status == _PASS else fail_detail if status == _FAIL else missing_detail,
        evidence=probe_evidence,
        remediation=remediation,
    )


def _status_with_evidence(
    status: ConnectorStatus | None,
    connector_id: str,
    evidence: dict[str, Any] | None,
) -> ConnectorStatus | None:
    if status is None:
        return None
    connector_evidence = _connector_evidence(evidence, connector_id)
    if not connector_evidence:
        return status

    diagnostics = _deep_merge(status.diagnostics, _dict_value(connector_evidence.get("diagnostics")))
    profile = _deep_merge(status.profile, _dict_value(connector_evidence.get("profile")))
    validation_patch = _dict_value(connector_evidence.get("validation"))
    if validation_patch:
        selected_profile = _dict_value(profile.get("selected_profile"))
        selected_profile["validation"] = _deep_merge(
            _dict_value(selected_profile.get("validation")),
            validation_patch,
        )
        profile["selected_profile"] = selected_profile
    return replace(status, diagnostics=diagnostics, profile=profile)


def _runtime_status_checks(manifest: ConnectorManifest, status: ConnectorStatus | None) -> list[CertificationCheck]:
    if status is None:
        return [
            _check(
                "runtime.status_surface",
                "Runtime status surface exists",
                scope="live",
                required=True,
                status=_MISSING,
                detail="Connector has no runtime status.",
                remediation="Add connector status mapping before certifying this provider.",
            )
        ]
    coherent = status.category == manifest.category and status.id == manifest.id and bool(status.state)
    state_issue = status.ready and status.state in {"not_configured", "not_detected", "not_available"}
    return [
        _check(
            "runtime.status_surface",
            "Runtime status surface exists",
            scope="live",
            required=True,
            status=_PASS if coherent else _FAIL,
            detail=(
                "Runtime status is present and category-scoped."
                if coherent
                else "Runtime status is malformed or category-mismatched."
            ),
            evidence={"state": status.state, "ready": status.ready, "connected": status.connected},
            remediation="Fix build_connector_statuses() mapping for this connector.",
        ),
        _check(
            "runtime.ready_state_semantics",
            "Ready state is semantically coherent",
            scope="live",
            required=True,
            status=_FAIL if state_issue else _PASS,
            detail=(
                "Ready/state flags are coherent."
                if not state_issue
                else "Connector is ready but reports an unavailable state."
            ),
            evidence={"state": status.state, "ready": status.ready, "reason": status.reason},
            remediation="Use ready/limited_ready/connected states consistently.",
        ),
    ]


def _validation_from_status(status: ConnectorStatus) -> dict[str, Any]:
    profile = status.profile
    selected_profile = profile.get("selected_profile")
    if isinstance(selected_profile, dict):
        validation = selected_profile.get("validation")
        return validation if isinstance(validation, dict) else {}
    return {}


def _probe_result(probe: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(probe, dict):
        return _MISSING, {}
    if probe.get("success") is True or probe.get("verified") is True or probe.get("status") == _PASS:
        return _PASS, probe
    if probe.get("success") is False or probe.get("verified") is False or probe.get("status") in {_FAIL, "failed"}:
        return _FAIL, probe
    return _MISSING, probe


def _validation_account_blocked(validation: dict[str, Any]) -> bool:
    return validation.get("account_blocked") is True


def _llm_live_checks(manifest: ConnectorManifest, status: ConnectorStatus | None) -> list[CertificationCheck]:
    if status is None:
        return []
    validation = _validation_from_status(status)
    account_blocked = _validation_account_blocked(validation)
    selected_profile = status.profile.get("selected_profile")
    has_selected_profile = isinstance(selected_profile, dict)
    has_api_key = bool(status.profile.get("has_api_key")) or (
        isinstance(selected_profile, dict) and bool(selected_profile.get("secrets", {}).get("api_key"))
    )
    local_or_managed = manifest.privacy_boundary in {"local", "managed_cloud"} or manifest.auth_type in {
        "managed",
        "subscription",
        "local_server",
    }
    credentials_ok = has_api_key or local_or_managed
    checks = [
        _check(
            "llm.live.credentials_or_local_runtime",
            "Credentials or local runtime are available",
            scope="live",
            required=True,
            status=_PASS if credentials_ok else _MISSING,
            detail=(
                "Connector has credentials or does not need cloud credentials."
                if credentials_ok
                else "Connector cannot be live-certified until credentials are configured."
            ),
            evidence={
                "has_selected_profile": has_selected_profile,
                "has_api_key": has_api_key,
                "auth_type": manifest.auth_type,
            },
            remediation="Save a provider profile with credentials, then run validation.",
        )
    ]

    validation_ran = "valid" in validation or account_blocked
    validation_valid = validation.get("valid") is True or account_blocked
    checks.append(
        _check(
            "llm.live.validation_chat",
            "Live validation chat succeeds",
            scope="live",
            required=True,
            status=_PASS if validation_valid else _FAIL if validation_ran else _MISSING,
            detail=(
                "Provider validation reached the named upstream but the account blocked generation."
                if account_blocked
                else (
                    "Provider validation succeeded."
                    if validation_valid
                    else "Provider validation failed." if validation_ran else "Provider validation has not been run."
                )
            ),
            evidence={
                "valid": validation.get("valid"),
                "account_blocked": account_blocked,
                "account_blocked_reason": validation.get("account_blocked_reason"),
                "latency_ms": validation.get("latency_ms"),
                "error_code": validation.get("error_code"),
            },
            remediation="Run profile validation from Settings or POST /v1/connectors/profiles/{id}/validate.",
        )
    )

    available_models = validation.get("available_models")
    available_model_count = validation.get("available_model_count")
    models_ok = isinstance(available_models, list) and bool(available_models)
    model_count_ok = isinstance(available_model_count, int) and available_model_count > 0
    default_models_ok = bool(manifest.default_models) and manifest.auth_type in {"managed", "subscription"}
    checks.append(
        _check(
            "llm.live.model_inventory",
            "Model inventory is known",
            scope="live",
            required=True,
            status=_PASS if models_ok or model_count_ok or default_models_ok else _MISSING,
            detail=(
                "Provider exposed or declared usable models."
                if models_ok or model_count_ok or default_models_ok
                else "Model inventory has not been proven."
            ),
            evidence={
                "available_models": available_models if isinstance(available_models, list) else [],
                "available_model_count": available_model_count,
            },
            remediation="Run model discovery/validation and store available_models in the profile validation payload.",
        )
    )

    tool_contract = validation.get("tool_contract") if isinstance(validation.get("tool_contract"), dict) else {}
    live_tool_probe = tool_contract.get("live_tool_probe")
    tool_probe_status = _MISSING
    tool_probe_evidence: dict[str, Any] = {"tool_contract": tool_contract}
    if isinstance(live_tool_probe, dict):
        tool_probe_status = _PASS if live_tool_probe.get("success") is True or account_blocked else _FAIL
        tool_probe_evidence = live_tool_probe
    elif live_tool_probe == "not_run":
        tool_probe_status = _MISSING
    checks.append(
        _check(
            "llm.live.tool_schema_contract",
            "Tool schema contract is proven",
            scope="live",
            required=True,
            status=tool_probe_status,
            detail=(
                "Provider reached the live tool probe but the account blocked generation."
                if account_blocked and tool_probe_status == _PASS
                else (
                    "Provider selected and returned the probe tool."
                    if tool_probe_status == _PASS
                    else (
                        "Tool schema probe failed."
                        if tool_probe_status == _FAIL
                        else "Tool schema probe has not been run."
                    )
                )
            ),
            evidence=tool_probe_evidence,
            remediation="Validate the profile with probe_tools=true before marking it certified.",
        )
    )

    command_probe = validation.get("command_route_probe")
    if not isinstance(command_probe, dict):
        command_probe = validation.get("command_probe")
    command_probe_status, command_probe_evidence = _probe_result(command_probe)
    checks.append(
        _check(
            "llm.live.command_route_probe",
            "Selected provider executes a Viola command turn",
            scope="live",
            required=True,
            status=command_probe_status,
            detail=(
                "Command route probe completed through this provider."
                if command_probe_status == _PASS
                else (
                    "Command route probe failed."
                    if command_probe_status == _FAIL
                    else "Command route probe has not been run for this provider."
                )
            ),
            evidence=command_probe_evidence,
            remediation="Run the provider certification command probe against this selected provider.",
        )
    )
    return checks


def _music_live_checks(manifest: ConnectorManifest, status: ConnectorStatus | None) -> list[CertificationCheck]:
    if status is None:
        return []
    account_optional = bool(manifest.capabilities.get("account_optional"))
    ready_ok = status.ready
    account_ok = status.connected or account_optional or manifest.auth_type == "local_path"
    auth_lifecycle_known = (
        any(action.id == "connect" for action in manifest.actions) or manifest.auth_type == "local_path"
    )
    playback_probe_status, playback_probe_evidence = _probe_result(
        status.diagnostics.get("last_playback_probe") or status.diagnostics.get("playback_probe")
    )
    return [
        _check(
            "music.live.ready_for_playback",
            "Selected runtime can play",
            scope="live",
            required=True,
            status=_PASS if ready_ok else _MISSING,
            detail=(
                "Music connector is ready for playback."
                if ready_ok
                else "Music connector is not ready for playback in this environment."
            ),
            evidence={"state": status.state, "ready": status.ready, "connected": status.connected},
            remediation="Connect, configure, or select a playable music source.",
        ),
        _check(
            "music.live.playback_probe",
            "Provider actually plays audio/video",
            scope="live",
            required=True,
            status=playback_probe_status,
            detail=(
                "Playback probe verified track load and position advance."
                if playback_probe_status == _PASS
                else (
                    "Playback probe failed."
                    if playback_probe_status == _FAIL
                    else "Playback probe has not been run for this provider."
                )
            ),
            evidence=playback_probe_evidence,
            remediation="Run viola-runner command --verify or connector gauntlet playback proof for this source.",
        ),
        _check(
            "music.live.account_boundary",
            "Account requirement is explicit",
            scope="live",
            required=True,
            status=_PASS if account_ok else _MISSING,
            detail=(
                "Account/session requirement is satisfied or explicitly optional."
                if account_ok
                else "Account/session is required but not connected."
            ),
            evidence={
                "connected": status.connected,
                "account_optional": account_optional,
                "auth_type": manifest.auth_type,
            },
            remediation="Sign in or mark only truly public playback paths account_optional.",
        ),
        _check(
            "music.live.auth_lifecycle_actions",
            "Auth lifecycle actions are available",
            scope="live",
            required=True,
            status=_PASS if auth_lifecycle_known else _FAIL,
            detail=(
                "User-visible auth/configuration action exists."
                if auth_lifecycle_known
                else "Connector has no visible connect/configure path."
            ),
            evidence={"actions": [action.id for action in manifest.actions]},
            remediation="Expose connect/configure actions through the connector manifest.",
        ),
    ]


def _smart_home_live_checks(manifest: ConnectorManifest, status: ConnectorStatus | None) -> list[CertificationCheck]:
    if status is None:
        return []
    if manifest.id == "smart_home.network_discovery":
        discovery_action = any(action.id == "discover" for action in manifest.actions)
        discovery_probe_status, discovery_probe_evidence = _probe_result(
            status.diagnostics.get("last_discovery_probe") or status.diagnostics.get("discovery_probe")
        )
        return [
            _check(
                "smart_home.live.discovery_action",
                "Discovery route is exposed",
                scope="live",
                required=True,
                status=_PASS if discovery_action else _FAIL,
                detail="Discovery action is available." if discovery_action else "Discovery action is missing.",
                evidence={"actions": [action.id for action in manifest.actions]},
            ),
            _check(
                "smart_home.live.discovery_probe",
                "Network discovery has been live-probed",
                scope="live",
                required=True,
                status=discovery_probe_status,
                detail=(
                    "Discovery probe found or definitively scanned the local network."
                    if discovery_probe_status == _PASS
                    else (
                        "Discovery probe failed."
                        if discovery_probe_status == _FAIL
                        else "Discovery probe has not been run in this environment."
                    )
                ),
                evidence=discovery_probe_evidence,
                remediation="Run the explicit smart-home discovery route and store probe evidence.",
            ),
        ]
    configured = status.connected and status.ready
    control_probe_status, control_probe_evidence = _probe_result(
        status.diagnostics.get("last_control_probe") or status.diagnostics.get("control_probe")
    )
    return [
        _check(
            "smart_home.live.configured_connection",
            "Hub connection is configured",
            scope="live",
            required=True,
            status=_PASS if configured else _MISSING,
            detail=(
                "Smart-home hub connection is configured."
                if configured
                else "Smart-home hub credentials are missing in this environment."
            ),
            evidence={"state": status.state, "profile": dict(status.profile)},
            remediation="Configure a Home Assistant URL and token, then test the connection.",
        ),
        _check(
            "smart_home.live.control_probe",
            "Hub control/read probe succeeds",
            scope="live",
            required=True,
            status=control_probe_status,
            detail=(
                "Smart-home hub probe completed successfully."
                if control_probe_status == _PASS
                else (
                    "Smart-home hub probe failed."
                    if control_probe_status == _FAIL
                    else "Smart-home hub probe has not been run."
                )
            ),
            evidence=control_probe_evidence,
            remediation="Run a harmless read/control probe before marking the hub certified.",
        ),
    ]


def _category_live_checks(manifest: ConnectorManifest, status: ConnectorStatus | None) -> list[CertificationCheck]:
    if manifest.category == "llm":
        return _llm_live_checks(manifest, status)
    if manifest.category == "music":
        return _music_live_checks(manifest, status)
    if manifest.category == "smart_home":
        return _smart_home_live_checks(manifest, status)
    return []


def _release_global_checks(evidence: dict[str, Any] | None) -> list[CertificationCheck]:
    release = _release_evidence(evidence)
    return [
        _probe_check(
            "release.auth_session_lifecycle",
            "Auth and session lifecycle is certified",
            release.get("auth_session_lifecycle"),
            scope="release",
            required=True,
            pass_detail="Account registration, login, logout, session revoke, password rotation, and edge paths passed.",
            missing_detail="Auth/session lifecycle proof is missing from release evidence.",
            fail_detail="Auth/session lifecycle proof failed.",
            remediation="Run tools/auth_session_gauntlet.py and merge its evidence before making a release claim.",
        ),
        _probe_check(
            "release.cross_user_isolation",
            "Cross-user isolation red team passed",
            release.get("cross_user_isolation"),
            scope="release",
            required=True,
            pass_detail="Cross-user negative probes found no unauthorized reads, writes, revokes, or inheritance.",
            missing_detail="Cross-user isolation proof is missing from release evidence.",
            fail_detail="Cross-user isolation proof failed.",
            remediation="Run tools/cross_user_redteam.py and merge its evidence before making a release claim.",
        ),
        _probe_check(
            "release.secret_leak_scan",
            "Evidence and artifacts have no secret leaks",
            release.get("secret_leak_scan"),
            scope="release",
            required=True,
            pass_detail="Secret/privacy leak scan found no raw credentials, cookies, or bearer tokens.",
            missing_detail="Secret/privacy leak scan proof is missing from release evidence.",
            fail_detail="Secret/privacy leak scan found sensitive material.",
            remediation="Remove leaked secrets from responses/logs/artifacts, rotate exposed credentials, and rerun release certification.",
        ),
        _probe_check(
            "release.race_restart",
            "Race and restart hardening probes passed",
            release.get("race_restart"),
            scope="release",
            required=True,
            pass_detail="Concurrent provider switching, music switching, and status refresh probes completed without corruption.",
            missing_detail="Race/restart hardening proof is missing from release evidence.",
            fail_detail="Race/restart hardening proof failed.",
            remediation="Run tools/race_restart_gauntlet.py and merge its evidence before making a release claim.",
        ),
    ]


def _release_connector_checks(
    manifest: ConnectorManifest,
    evidence: dict[str, Any] | None,
) -> list[CertificationCheck]:
    connector_evidence = _connector_evidence(evidence, manifest.id)
    checks: list[CertificationCheck] = []
    if manifest.category in {"llm", "music"}:
        checks.append(
            _probe_check(
                "release.provider_switch_permutations",
                "Provider switching permutations are certified",
                connector_evidence.get("provider_switch_permutations")
                or connector_evidence.get("switching")
                or connector_evidence.get("profile_switching"),
                scope="release",
                required=True,
                pass_detail="Switching to and from this provider preserved selected state and unrelated provider auth state.",
                missing_detail="Provider switching proof is missing for this connector.",
                fail_detail="Provider switching proof failed for this connector.",
                remediation="Run the connector gauntlet switching matrix for this connector and merge its evidence.",
            )
        )

    has_connection_lifecycle = any(action.id in {"connect", "disconnect", "revoke"} for action in manifest.actions)
    if manifest.category == "llm" or has_connection_lifecycle:
        checks.append(
            _probe_check(
                "release.disconnect_reconnect",
                "Disconnect/reconnect or profile delete/reselect behavior is certified",
                connector_evidence.get("disconnect_reconnect")
                or connector_evidence.get("profile_delete_reselect")
                or connector_evidence.get("connection_lifecycle"),
                scope="release",
                required=True,
                pass_detail="Disconnect/reconnect or profile delete/reselect behavior preserved boundaries.",
                missing_detail="Disconnect/reconnect proof is missing for this connector.",
                fail_detail="Disconnect/reconnect proof failed for this connector.",
                remediation="Run the connector lifecycle gauntlet for this connector and merge its evidence.",
            )
        )
    return checks


def _build_certification(
    manifest: ConnectorManifest,
    status: ConnectorStatus | None,
    *,
    evidence: dict[str, Any] | None = None,
) -> ConnectorCertification:
    status = _status_with_evidence(status, manifest.id, evidence)
    checks = [
        *_manifest_static_checks(manifest),
        *_category_static_checks(manifest),
        *_runtime_status_checks(manifest, status),
        *_category_live_checks(manifest, status),
        *_release_global_checks(evidence),
        *_release_connector_checks(manifest, evidence),
    ]
    tier, claim = _tier_for_checks(checks)
    required_passed, required_total, required_missing, required_failed = _required_summary(checks)
    return ConnectorCertification(
        connector_id=manifest.id,
        category=manifest.category,
        tier=tier,
        claim=claim,
        required_passed=required_passed,
        required_total=required_total,
        required_missing=required_missing,
        required_failed=required_failed,
        checks=tuple(checks),
    )


def _summary(
    certifications: list[ConnectorCertification],
    *,
    scoped: bool,
    expected_total: int,
) -> dict[str, Any]:
    required_total = sum(cert.required_total for cert in certifications)
    required_passed = sum(cert.required_passed for cert in certifications)
    required_missing = sum(cert.required_missing for cert in certifications)
    required_failed = sum(cert.required_failed for cert in certifications)
    by_tier: dict[str, int] = {}
    by_category: dict[str, dict[str, int]] = {}
    for cert in certifications:
        by_tier[cert.tier] = by_tier.get(cert.tier, 0) + 1
        category_counts = by_category.setdefault(cert.category, {})
        category_counts[cert.tier] = category_counts.get(cert.tier, 0) + 1
    # A 100% claim is only honest against the FULL connector inventory. A
    # category- or connector-scoped run shrinks the denominator, so it can
    # never make the release claim no matter how green it is.
    full_coverage = not scoped and len(certifications) == expected_total
    checks_all_passed = bool(certifications) and required_total > 0 and required_passed == required_total
    can_claim = full_coverage and checks_all_passed
    if can_claim:
        claim = "100% of certified provider contract checks passed."
    elif checks_all_passed and not full_coverage:
        claim = (
            "Cannot claim 100% certification: this run is scoped to a subset of "
            "connectors; the claim requires the full connector inventory."
        )
    else:
        claim = "Cannot claim 100% certification: required checks are missing or failing."
    return {
        "connectors_total": len(certifications),
        "connectors_expected": expected_total,
        "scoped": scoped,
        "full_coverage": full_coverage,
        "required_total": required_total,
        "required_passed": required_passed,
        "required_missing": required_missing,
        "required_failed": required_failed,
        "required_percent": round((required_passed / required_total) * 100, 2) if required_total else 0.0,
        "by_tier": by_tier,
        "by_category": by_category,
        "release_claim": {
            "can_claim_100_percent": can_claim,
            "claim": claim,
        },
    }


async def build_connector_certification_payload(
    user_id: str,
    category: str | None = None,
    connector_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the certification matrix for a concrete user/environment."""

    normalized_category = (category or "").strip().lower() or None
    normalized_connector_id = (connector_id or "").strip().lower() or None
    scoped = normalized_category is not None or normalized_connector_id is not None
    expected_total = len(list_connector_manifests(None))
    manifests = (
        [manifest]
        if normalized_connector_id and (manifest := get_connector_manifest(normalized_connector_id)) is not None
        else list_connector_manifests(normalized_category)
    )
    if normalized_connector_id and not manifests:
        manifests = []
    statuses = _status_by_id(await build_connector_statuses(user_id, normalized_category))
    certifications = [
        _build_certification(manifest, statuses.get(manifest.id), evidence=evidence) for manifest in manifests
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "certification_schema_version": CERTIFICATION_SCHEMA_VERSION,
        "contract_version": CERTIFICATION_CONTRACT_VERSION,
        "category": normalized_category,
        "connector_id": normalized_connector_id,
        "certifications": [cert.to_dict() for cert in certifications],
        "summary": _summary(certifications, scoped=scoped, expected_total=expected_total),
    }
