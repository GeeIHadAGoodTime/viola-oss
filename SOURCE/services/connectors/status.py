"""Runtime connector status builders.

This module intentionally reads existing state without mutating credentials,
selection, browser sessions, or local services.  It is the bridge from today's
settings/provider implementations to the connector architecture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import defaults
from services.connectors.manifests import SCHEMA_VERSION, get_connector_manifest, list_connector_manifests
from services.connectors.models import ConnectionProfile, ConnectorManifest, ConnectorStatus

_MUSIC_PROVIDER_ALIASES = {
    "youtube": "youtube_music",
    "youtube_iframe": "youtube_music",
    "ytmusic": "youtube_music",
    "spotify_cdp": "spotify",
    "spotify": "spotify",
    "local_files": "local",
    "local": "local",
}
_LOCAL_BASE_URL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # nosec B104


def _string_value(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _clean_base_url(value: object) -> str:
    return _string_value(value).strip().rstrip("/")


def _is_local_base_url(value: object) -> bool:
    candidate = _clean_base_url(value)
    if not candidate:
        return False
    if "://" not in candidate:
        candidate = "http://%s" % candidate
    parsed = urlparse(candidate)
    return (parsed.hostname or "").lower() in _LOCAL_BASE_URL_HOSTS


def _key_present(value: object) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped and stripped not in {"***ENCRYPTED***", "••••••"})


def _canonical_music_provider(provider_id: object) -> str:
    raw = _string_value(provider_id).strip().lower()
    return _MUSIC_PROVIDER_ALIASES.get(raw, raw)


def _manifest_base_url(manifest: ConnectorManifest) -> str:
    return (manifest.default_base_url or "").strip().rstrip("/")


def _base_urls_match(left: object, right: object) -> bool:
    return _clean_base_url(left).lower() == _clean_base_url(right).lower()


def _local_probe_base_url(
    manifest: ConnectorManifest,
    llm_settings: dict[str, str],
    active_profile: ConnectionProfile | None = None,
) -> str:
    if active_profile is not None and active_profile.base_url:
        return _clean_base_url(active_profile.base_url)
    if manifest.id == "llm.openai_compatible" and llm_settings["ai_source"].strip().lower() == "local":
        return _clean_base_url(llm_settings["llm_base_url"])
    return _manifest_base_url(manifest)


def _uses_local_llm_probe(
    manifest: ConnectorManifest,
    llm_settings: dict[str, str],
    active_profile: ConnectionProfile | None = None,
) -> bool:
    if "local" in manifest.tags:
        return True
    if manifest.id != "llm.openai_compatible":
        return False
    base_url = active_profile.base_url if active_profile is not None else llm_settings["llm_base_url"]
    return llm_settings["ai_source"].strip().lower() == "local" or _is_local_base_url(base_url)


def _settings_manager() -> Any:
    from ui.settings_manager import get_settings_manager

    return get_settings_manager()


def _connection_profile_store() -> Any:
    from services.connectors.profiles import get_connection_profile_store

    return get_connection_profile_store()


def _current_llm_settings(user_id: str) -> dict[str, str]:
    settings = _settings_manager()
    return {
        "ai_source": _string_value(settings.get("ai_source", defaults.DEFAULT_AI_SOURCE, user_id=user_id), "managed"),
        "llm_provider": _string_value(settings.get("llm_provider", "openai", user_id=user_id), "openai"),
        "llm_model": _string_value(settings.get("llm_model", "", user_id=user_id)),
        "llm_base_url": _clean_base_url(settings.get("llm_base_url", "", user_id=user_id)),
        "llm_api_key": _string_value(settings.get("llm_api_key", "", user_id=user_id)),
    }


def _selected_llm_connector_id(llm_settings: dict[str, str]) -> str:
    ai_source = llm_settings["ai_source"].strip().lower()
    provider = llm_settings["llm_provider"].strip().lower()
    base_url = llm_settings["llm_base_url"]

    if ai_source == "managed":
        return "llm.managed"
    if ai_source == "codex":
        return "llm.codex"
    if ai_source == "local":
        if provider == "ollama":
            return "llm.ollama"
        for manifest in list_connector_manifests("llm"):
            if (
                "local" in manifest.tags
                and manifest.adapter == provider
                and _base_urls_match(base_url, manifest.default_base_url)
            ):
                return manifest.id
        return "llm.openai_compatible"
    if provider in {"openai", "anthropic", "google", "ollama"}:
        return "llm.%s" % provider
    for manifest in list_connector_manifests("llm"):
        if (
            "byok" in manifest.tags
            and manifest.adapter == "openai_compatible"
            and _base_urls_match(
                base_url,
                manifest.default_base_url,
            )
        ):
            return manifest.id
    return "llm.openai_compatible"


def _managed_llm_available() -> bool:
    try:
        from config.settings import settings as app_settings

        return bool(getattr(app_settings, "openai_api_key", ""))
    except Exception:
        return False


def _codex_available() -> bool:
    try:
        from services.llm.codex_auth import is_codex_available

        return bool(is_codex_available())
    except Exception:
        return False


def _local_server_index(local_servers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for server in local_servers:
        if not isinstance(server, dict):
            continue
        url = _clean_base_url(server.get("url"))
        if url:
            indexed[url.lower()] = server
        server_type = _string_value(server.get("type")).lower()
        if server_type and server_type not in indexed:
            indexed[server_type] = server
    return indexed


def _profiles_by_connector(profiles: list[ConnectionProfile]) -> dict[str, list[ConnectionProfile]]:
    grouped: dict[str, list[ConnectionProfile]] = {}
    for profile in profiles:
        grouped.setdefault(profile.connector_id, []).append(profile)
    return grouped


def _profile_status_payloads(
    profiles: list[ConnectionProfile],
    *,
    selected_profile: ConnectionProfile | None,
) -> list[dict[str, Any]]:
    selected_id = selected_profile.profile_id if selected_profile else ""
    return [profile.to_dict(selected=profile.profile_id == selected_id) for profile in profiles]


def _llm_status_for_manifest(
    manifest: ConnectorManifest,
    *,
    llm_settings: dict[str, str],
    selected_connector_id: str,
    selected_profile_id: str | None,
    selected_profile: ConnectionProfile | None,
    connector_profiles: list[ConnectionProfile],
    profile_store: Any | None,
    local_servers: list[dict[str, Any]],
) -> ConnectorStatus:
    selected = (
        selected_profile.connector_id == manifest.id
        if selected_profile is not None
        else manifest.id == selected_connector_id
    )
    connected = False
    ready = False
    state = "not_configured"
    reason = "Not configured."
    diagnostics: dict[str, Any] = {
        "adapter": manifest.adapter,
        "provider_key": manifest.provider_key,
        "profile_scope": "connection_profiles" if connector_profiles else "single_active_settings_key",
    }
    profile: dict[str, Any] = {
        "saved_profiles": _profile_status_payloads(connector_profiles, selected_profile=selected_profile)
    }

    active_profile = selected_profile if selected_profile and selected_profile.connector_id == manifest.id else None
    if active_profile is None and connector_profiles:
        active_profile = connector_profiles[0]

    if active_profile is not None and profile_store is not None:
        has_key = bool(profile_store.profile_has_secret(active_profile, "api_key"))
        profile.update(
            {
                "selected_profile": active_profile.to_dict(selected=selected_profile == active_profile),
                "profile_count": len(connector_profiles),
                "has_api_key": has_key,
            }
        )
        if not active_profile.enabled:
            connected = False
            ready = False
            state = "disabled"
            reason = "Selected provider profile is disabled." if selected else "Saved provider profile is disabled."
        elif manifest.id in {"llm.managed", "llm.codex"}:
            connected = True
            ready = True
            state = "ready"
            reason = "Saved profile is selected." if selected else "Saved profile is configured."
        elif _uses_local_llm_probe(manifest, llm_settings, active_profile):
            indexed = _local_server_index(local_servers)
            server = indexed.get(_local_probe_base_url(manifest, llm_settings, active_profile).lower())
            if server is None and manifest.provider_key == "ollama":
                server = indexed.get("ollama")
            running = bool(server.get("running")) if isinstance(server, dict) else False
            models = server.get("models", []) if isinstance(server, dict) else []
            validation = active_profile.validation
            validation_ok = validation.get("valid") is True
            connected = True
            ready = running or validation_ok
            state = "ready" if ready else "saved_not_running"
            reason = (
                "Saved local profile is reachable."
                if ready
                else "Saved local profile exists, but the local server is not currently detected."
            )
            profile["selected_profile"]["detected_models"] = models if isinstance(models, list) else []
            profile["selected_profile"]["running"] = running
        else:
            connected = has_key
            validation = active_profile.validation
            validation_failed = validation.get("valid") is False
            ready = connected and not validation_failed
            if ready:
                state = "ready"
                reason = "Saved provider profile has credentials."
            elif connected:
                state = "validation_failed"
                reason = _string_value(validation.get("message"), "Saved provider profile validation failed.")
            else:
                state = "missing_credentials"
                reason = "Saved provider profile is missing credentials."
        return ConnectorStatus(
            id=manifest.id,
            category=manifest.category,
            connected=connected,
            selected=selected,
            ready=ready,
            state=state,
            reason=reason,
            status_source="connection_profiles+settings_manager+local_probe",
            capabilities=manifest.capabilities,
            diagnostics=diagnostics,
            profile=profile,
            actions=manifest.actions,
        )

    if manifest.id == "llm.managed":
        connected = _managed_llm_available()
        ready = connected
        state = "ready" if ready else "not_configured"
        reason = "Managed OpenAI key is configured." if ready else "Managed OpenAI key is not configured."
    elif manifest.id == "llm.codex":
        connected = _codex_available()
        ready = connected
        state = "ready" if ready else "not_configured"
        reason = "Codex auth is available." if ready else "Codex auth is not available."
    elif _uses_local_llm_probe(manifest, llm_settings):
        indexed = _local_server_index(local_servers)
        probe_base_url = _local_probe_base_url(manifest, llm_settings)
        server = indexed.get(probe_base_url.lower())
        if server is None and manifest.provider_key == "ollama":
            server = indexed.get("ollama")
        models = server.get("models", []) if isinstance(server, dict) else []
        running = bool(server.get("running")) if isinstance(server, dict) else False
        connected = bool(server and (running or models))
        ready = bool(server and running)
        state = "ready" if ready else ("installed" if connected else "not_detected")
        reason = (
            "Local server is running."
            if ready
            else (
                "Installed models were found, but the server is not running."
                if connected
                else "Local server was not detected."
            )
        )
        profile = {
            "base_url": probe_base_url,
            "models": models if isinstance(models, list) else [],
            "detected_from": server.get("detected_from") if isinstance(server, dict) else None,
            "running": running,
            "saved_profiles": profile["saved_profiles"],
        }
    else:
        key_present = _key_present(llm_settings["llm_api_key"])
        is_selected_byok = selected and llm_settings["ai_source"] == "byok"
        connected = bool(is_selected_byok and key_present)
        ready = connected
        if connected:
            state = "ready"
            reason = "Selected BYOK profile has an API key."
        elif selected and not key_present:
            state = "missing_credentials"
            reason = "Selected BYOK provider is missing an API key."
        else:
            state = "not_configured"
            reason = "No per-provider profile exists yet; current settings support one active BYOK key."
        profile = {
            "base_url": manifest.default_base_url,
            "model": llm_settings["llm_model"] if selected else "",
            "has_api_key": connected,
            "saved_profiles": profile["saved_profiles"],
        }

    if selected:
        profile["selected_settings"] = {
            "ai_source": llm_settings["ai_source"],
            "llm_provider": llm_settings["llm_provider"],
            "llm_model": llm_settings["llm_model"],
            "llm_base_url": llm_settings["llm_base_url"],
            "has_api_key": _key_present(llm_settings["llm_api_key"]),
        }

    return ConnectorStatus(
        id=manifest.id,
        category=manifest.category,
        connected=connected,
        selected=selected,
        ready=ready,
        state=state,
        reason=reason,
        status_source="settings_manager+local_probe",
        capabilities=manifest.capabilities,
        diagnostics=diagnostics,
        profile=profile,
        actions=manifest.actions,
    )


def _browser_auth_statuses(user_id: str) -> dict[str, dict[str, Any]]:
    try:
        from music.providers.browser.auth_manager import get_browser_auth_manager

        manager = get_browser_auth_manager(user_id)
        statuses = manager.get_auth_status()
        return {
            key: status.to_dict() if hasattr(status, "to_dict") else {}
            for key, status in statuses.items()
            if isinstance(key, str)
        }
    except Exception:
        return {}


def _consent_statuses(user_id: str) -> dict[str, dict[str, Any]]:
    try:
        from music.consent import get_consent_service

        return {status.provider_id: status.to_dict() for status in get_consent_service().list_statuses(user_id=user_id)}
    except Exception:
        return {}


def _local_library_profile(user_id: str) -> dict[str, Any]:
    settings = _settings_manager()
    path = _string_value(settings.get("local_music_folder", "", user_id=user_id)).strip()
    exists = False
    if path:
        try:
            exists = Path(path).exists()
        except OSError:
            exists = False
    return {"path": path, "exists": exists}


def _music_status_for_manifest(
    manifest: ConnectorManifest,
    *,
    user_id: str,
    selected_provider_id: str,
    consent: dict[str, dict[str, Any]],
    browser_auth: dict[str, dict[str, Any]],
) -> ConnectorStatus:
    provider_key = manifest.provider_key
    selected = provider_key == selected_provider_id
    profile: dict[str, Any] = {}

    if provider_key == "local":
        local_profile = _local_library_profile(user_id)
        connected = bool(local_profile["path"] and local_profile["exists"])
        ready = connected
        state = "ready" if ready else "not_configured"
        reason = "Local music folder exists." if ready else "Choose a local music folder."
        profile = local_profile
    else:
        consent_status = consent.get(provider_key, {})
        browser_status = browser_auth.get(provider_key, {})
        state_text = _string_value(consent_status.get("state")).lower()
        linked = state_text == "linked"
        logged_in = bool(browser_status.get("logged_in"))
        connected = linked or logged_in
        account_optional = bool(manifest.capabilities.get("account_optional"))
        ready = connected or account_optional
        if connected:
            state = "ready"
            reason = "Account/session is connected."
        elif ready:
            state = "limited_ready"
            reason = "%s can play public content; sign in for account-specific features." % manifest.display_name
        elif consent_status.get("authorization_url") or any(action.id == "connect" for action in manifest.actions):
            state = "sign_in_available"
            reason = "Sign-in is available."
        else:
            state = "not_available"
            reason = "No sign-in path is currently available."
        profile = {
            "consent": consent_status,
            "browser_auth": browser_status,
        }

    if selected and not ready:
        reason = "%s is selected, but it is not ready yet." % manifest.display_name

    return ConnectorStatus(
        id=manifest.id,
        category=manifest.category,
        connected=connected,
        selected=selected,
        ready=ready,
        state=state,
        reason=reason,
        status_source="settings_manager+consent+browser_auth",
        capabilities=manifest.capabilities,
        diagnostics={"canonical_provider_id": provider_key},
        profile=profile,
        actions=manifest.actions,
    )


def _smart_home_status_for_manifest(manifest: ConnectorManifest, *, user_id: str) -> ConnectorStatus:
    settings = _settings_manager()
    if manifest.id == "smart_home.network_discovery":
        enabled = bool(settings.get("network_discovery_enabled", False, user_id=user_id))
        return ConnectorStatus(
            id=manifest.id,
            category=manifest.category,
            connected=enabled,
            selected=enabled,
            ready=enabled,
            state="enabled" if enabled else "disabled",
            reason="Network discovery is enabled." if enabled else "Network discovery is disabled.",
            status_source="settings_manager",
            capabilities=manifest.capabilities,
            profile={"network_discovery_enabled": enabled},
            actions=manifest.actions,
        )

    url = _string_value(settings.get("home_assistant_url", "", user_id=user_id)).strip()
    token_present = _key_present(settings.get("home_assistant_token", "", user_id=user_id))
    connected = bool(url and token_present)
    return ConnectorStatus(
        id=manifest.id,
        category=manifest.category,
        connected=connected,
        selected=connected,
        ready=connected,
        state="configured" if connected else "not_configured",
        reason="Home Assistant URL and token are configured." if connected else "Home Assistant URL/token are missing.",
        status_source="settings_manager",
        capabilities=manifest.capabilities,
        profile={"has_url": bool(url), "has_token": token_present, "url": url if url else ""},
        actions=manifest.actions,
    )


async def _detect_local_servers_if_needed(category: str | None) -> list[dict[str, Any]]:
    if category not in {None, "", "llm"}:
        return []
    try:
        from services.llm.local_models import detect_local_ai_servers

        return await detect_local_ai_servers()
    except Exception:
        return []


def _status_summary(statuses: list[ConnectorStatus]) -> dict[str, int]:
    return {
        "total": len(statuses),
        "connected": sum(1 for status in statuses if status.connected),
        "selected": sum(1 for status in statuses if status.selected),
        "ready": sum(1 for status in statuses if status.ready),
    }


async def build_connector_statuses(user_id: str, category: str | None = None) -> list[ConnectorStatus]:
    normalized_category = (category or "").strip().lower() or None
    manifests = list_connector_manifests(normalized_category)
    local_servers = await _detect_local_servers_if_needed(normalized_category)
    llm_settings = _current_llm_settings(user_id)
    selected_llm = _selected_llm_connector_id(llm_settings)
    settings = _settings_manager()
    selected_music = _canonical_music_provider(settings.get("active_music_provider_id", None, user_id=user_id))
    consent = _consent_statuses(user_id) if normalized_category in {None, "music"} else {}
    browser_auth = _browser_auth_statuses(user_id) if normalized_category in {None, "music"} else {}
    profile_store = _connection_profile_store()
    try:
        llm_profiles = profile_store.list_profiles(user_id, "llm") if normalized_category in {None, "llm"} else []
        selected_llm_profile_id = (
            profile_store.get_selected_profile_id(user_id, "llm") if normalized_category in {None, "llm"} else None
        )
        selected_llm_profile = (
            profile_store.get_profile(user_id, selected_llm_profile_id)
            if selected_llm_profile_id and normalized_category in {None, "llm"}
            else None
        )
    except Exception:
        llm_profiles = []
        selected_llm_profile_id = None
        selected_llm_profile = None
    if selected_llm_profile_id and selected_llm_profile is None:
        selected_llm = ""
    llm_profiles_by_connector = _profiles_by_connector(llm_profiles)

    statuses: list[ConnectorStatus] = []
    for manifest in manifests:
        if manifest.category == "llm":
            statuses.append(
                _llm_status_for_manifest(
                    manifest,
                    llm_settings=llm_settings,
                    selected_connector_id=selected_llm,
                    selected_profile_id=selected_llm_profile_id,
                    selected_profile=selected_llm_profile,
                    connector_profiles=llm_profiles_by_connector.get(manifest.id, []),
                    profile_store=profile_store,
                    local_servers=local_servers,
                )
            )
        elif manifest.category == "music":
            statuses.append(
                _music_status_for_manifest(
                    manifest,
                    user_id=user_id,
                    selected_provider_id=selected_music,
                    consent=consent,
                    browser_auth=browser_auth,
                )
            )
        elif manifest.category == "smart_home":
            statuses.append(_smart_home_status_for_manifest(manifest, user_id=user_id))
    return statuses


async def build_connector_status_payload(user_id: str, category: str | None = None) -> dict[str, Any]:
    statuses = await build_connector_statuses(user_id, category)
    return {
        "schema_version": SCHEMA_VERSION,
        "connectors": [status.to_dict() for status in statuses],
        "summary": _status_summary(statuses),
    }


async def build_music_sources_payload(user_id: str) -> dict[str, Any]:
    statuses = await build_connector_statuses(user_id, "music")
    selected = next((status for status in statuses if status.selected), None)
    return {
        "schema_version": SCHEMA_VERSION,
        "active_source_id": selected.id.removeprefix("music.") if selected else None,
        "sources": [status.to_dict() for status in statuses],
        "summary": _status_summary(statuses),
    }


def select_music_source(user_id: str, source_id: str) -> dict[str, Any]:
    canonical = _canonical_music_provider(source_id)
    manifest = get_connector_manifest("music.%s" % canonical)
    if manifest is None:
        raise ValueError("Unknown music source: %s" % source_id)
    settings = _settings_manager()
    settings.set_user_setting(user_id, "active_music_provider_id", canonical)
    return {"source_id": canonical, "selected": True}
