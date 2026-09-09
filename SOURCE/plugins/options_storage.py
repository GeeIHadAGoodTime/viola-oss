"""Plugin user-config / options storage (F-022).

Claude `userConfig` declares typed plugin options. Non-sensitive values
live in settings; sensitive values live in secure storage; both can be
referenced as ``${user_config.KEY}`` in MCP / LSP / hook / channel
configs. Sensitive values are withheld from skill/agent prose.

Viola desktop runtime uses ``services.security.secret_store`` for the
sensitive layer; cloud runtime should not import this module at all,
so storage rooted at ``get_data_dir()`` stays desktop-only by
construction.

Headless-keychain hatch (#757 / #2683 / #2715): the ``import keyring``
fallback below (used only when ``services.security.secret_store`` is
unavailable) is guarded by
:func:`utils.enhancements.secrets.is_os_keyring_disabled` before it ever
touches the OS keychain, so a headless/test run with
``VIOLA_DISABLE_OS_KEYRING`` set degrades straight to "no secret store
available" instead of blocking on a macOS SecurityAgent authorization
dialog. See ``services/memory/key_provider.py`` for the sanctioned
pattern this mirrors, and ``scripts/check_keyring_hatch_bypass.py`` for
the ratchet gate that enforces it repo-wide.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir
from utils.enhancements.secrets import is_os_keyring_disabled

logger = get_logger(__name__)


SENSITIVE_PLACEHOLDER = "<redacted-sensitive>"
_SUBSTITUTION_RE = re.compile(r"\$\{user_config\.([A-Za-z0-9_.\-]+)\}")


@dataclass(frozen=True)
class OptionSchemaField:
    name: str
    type: str = "string"
    default: Any = None
    sensitive: bool = False
    required: bool = False
    description: str = ""

    @classmethod
    def from_spec(cls, name: str, spec: Any) -> OptionSchemaField:
        if isinstance(spec, dict):
            return cls(
                name=name,
                type=str(spec.get("type") or "string"),
                default=spec.get("default"),
                sensitive=bool(spec.get("sensitive", False)),
                required=bool(spec.get("required", False)),
                description=str(spec.get("description") or ""),
            )
        return cls(name=name, default=spec)


def parse_user_config_schema(raw: dict[str, Any] | None) -> dict[str, OptionSchemaField]:
    if not raw or not isinstance(raw, dict):
        return {}
    return {name: OptionSchemaField.from_spec(name, spec) for name, spec in raw.items()}


def _secret_store_set(plugin_name: str, key: str, value: str) -> bool:
    """Persist a sensitive value in the desktop secret store, if available."""
    try:
        from services.security.secret_store import set_secret

        set_secret("plugin:%s:%s" % (plugin_name, key), value)
        return True
    except ImportError:
        logger.debug("services.security.secret_store unavailable, trying keyring")
    except OSError as exc:
        logger.warning("secret_store set failed for %s.%s: %s", plugin_name, key, exc)
        return False
    if is_os_keyring_disabled():
        return False
    try:  # pragma: no cover - keyring fallback
        import keyring

        keyring.set_password("viola.plugin.%s" % plugin_name, key, value)
        return True
    except ImportError:
        return False
    except OSError as exc:  # pragma: no cover
        logger.warning("keyring set failed for %s.%s: %s", plugin_name, key, exc)
        return False


def _secret_store_get(plugin_name: str, key: str) -> str | None:
    try:
        from services.security.secret_store import get_secret

        return get_secret("plugin:%s:%s" % (plugin_name, key))
    except ImportError:
        logger.debug("services.security.secret_store unavailable, trying keyring")
    except OSError as exc:
        logger.warning("secret_store get failed for %s.%s: %s", plugin_name, key, exc)
        return None
    if is_os_keyring_disabled():
        return None
    try:  # pragma: no cover
        import keyring

        return keyring.get_password("viola.plugin.%s" % plugin_name, key)
    except ImportError:
        return None
    except OSError as exc:  # pragma: no cover
        logger.warning("keyring get failed for %s.%s: %s", plugin_name, key, exc)
        return None


def _secret_store_delete(plugin_name: str, key: str) -> bool:
    try:
        from services.security.secret_store import delete_secret

        delete_secret("plugin:%s:%s" % (plugin_name, key))
        return True
    except ImportError:
        logger.debug("services.security.secret_store unavailable, trying keyring")
    except OSError as exc:
        logger.warning("secret_store delete failed for %s.%s: %s", plugin_name, key, exc)
        return False
    if is_os_keyring_disabled():
        return False
    try:  # pragma: no cover
        import keyring

        keyring.delete_password("viola.plugin.%s" % plugin_name, key)
        return True
    except ImportError:
        return False
    except OSError as exc:  # pragma: no cover
        logger.warning("keyring delete failed for %s.%s: %s", plugin_name, key, exc)
        return False


class PluginOptionsStorage:
    """Resolve and persist Claude `userConfig` plugin options."""

    def __init__(self, plugin_base_dirs: list[Path], options_path: Path | None = None) -> None:
        self._base_dirs = plugin_base_dirs
        if options_path is None:
            options_path = get_data_dir() / "plugin_options.json"
        self._options_path = options_path
        self._public: dict[str, dict[str, Any]] = {}
        self._sensitive_keys: dict[str, set[str]] = {}
        self.reload()

    def reload(self) -> None:
        if not self._options_path.exists():
            return
        try:
            with open(self._options_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read plugin_options.json: %s", exc)
            return
        for plugin_name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            self._public[plugin_name] = dict(entry.get("public") or {})
            self._sensitive_keys[plugin_name] = set(entry.get("sensitive_keys") or [])

    def save(self) -> None:
        try:
            self._options_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, dict[str, Any]] = {}
            for plugin_name in set(self._public) | set(self._sensitive_keys):
                payload[plugin_name] = {
                    "public": self._public.get(plugin_name, {}),
                    "sensitive_keys": sorted(self._sensitive_keys.get(plugin_name, set())),
                }
            with open(self._options_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
        except OSError as exc:
            logger.warning("Failed to write plugin_options.json: %s", exc)

    def set_option(
        self,
        plugin_name: str,
        key: str,
        value: Any,
        *,
        sensitive: bool = False,
    ) -> bool:
        if sensitive:
            stored = _secret_store_set(plugin_name, key, "" if value is None else str(value))
            if not stored:
                logger.warning(
                    "Refusing to store sensitive plugin option %s.%s in plaintext; "
                    "desktop secret store is not available",
                    plugin_name,
                    key,
                )
                return False
            self._sensitive_keys.setdefault(plugin_name, set()).add(key)
            # Make sure no stale plaintext lingers
            self._public.setdefault(plugin_name, {}).pop(key, None)
        else:
            self._public.setdefault(plugin_name, {})[key] = value
            sensitive_keys = self._sensitive_keys.get(plugin_name)
            if sensitive_keys is not None:
                sensitive_keys.discard(key)
        self.save()
        return True

    def get_option(
        self,
        plugin_name: str,
        key: str,
        *,
        reveal_sensitive: bool = False,
    ) -> Any:
        if key in self._sensitive_keys.get(plugin_name, set()):
            if not reveal_sensitive:
                return SENSITIVE_PLACEHOLDER
            return _secret_store_get(plugin_name, key)
        return self._public.get(plugin_name, {}).get(key)

    def get_all_options(
        self,
        plugin_name: str,
        *,
        reveal_sensitive: bool = False,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = dict(self._public.get(plugin_name, {}))
        for key in self._sensitive_keys.get(plugin_name, set()):
            if reveal_sensitive:
                merged[key] = _secret_store_get(plugin_name, key)
            else:
                merged[key] = SENSITIVE_PLACEHOLDER
        return merged

    def delete_option(self, plugin_name: str, key: str) -> None:
        sensitive_keys = self._sensitive_keys.get(plugin_name)
        if sensitive_keys is not None and key in sensitive_keys:
            _secret_store_delete(plugin_name, key)
            sensitive_keys.discard(key)
        self._public.get(plugin_name, {}).pop(key, None)
        self.save()

    def ensure_defaults(
        self,
        plugin_name: str,
        schema: dict[str, OptionSchemaField],
    ) -> None:
        """Seed plugin options from the manifest's userConfig schema.

        Existing values (sensitive or public) are NEVER overwritten.
        Sensitive defaults are skipped — Claude never ships a default
        secret; the user installs the value at enable time.
        """
        for name, field_def in schema.items():
            if field_def.sensitive:
                continue
            if field_def.default is None:
                continue
            current = self._public.get(plugin_name, {})
            if name in current:
                continue
            current[name] = field_def.default
            self._public[plugin_name] = current
        self.save()

    def substitute(
        self,
        text: str,
        plugin_name: str,
        *,
        reveal_sensitive: bool,
    ) -> str:
        """Substitute ``${user_config.KEY}`` markers in ``text``.

        Args:
            reveal_sensitive: When ``True``, sensitive values are
                injected as their real secret. When ``False``, sensitive
                values are replaced with :data:`SENSITIVE_PLACEHOLDER`.
                MCP / LSP launch configs pass ``reveal_sensitive=True``;
                skill / agent prose passes ``reveal_sensitive=False``.
        """

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in self._sensitive_keys.get(plugin_name, set()):
                if not reveal_sensitive:
                    return SENSITIVE_PLACEHOLDER
                value = _secret_store_get(plugin_name, key)
            else:
                value = self._public.get(plugin_name, {}).get(key)
            if value is None:
                return match.group(0)
            return str(value)

        return _SUBSTITUTION_RE.sub(_replace, text)

    def substitute_payload(
        self,
        payload: Any,
        plugin_name: str,
        *,
        reveal_sensitive: bool,
    ) -> Any:
        if isinstance(payload, str):
            return self.substitute(payload, plugin_name, reveal_sensitive=reveal_sensitive)
        if isinstance(payload, dict):
            return {
                k: self.substitute_payload(v, plugin_name, reveal_sensitive=reveal_sensitive)
                for k, v in payload.items()
            }
        if isinstance(payload, list):
            return [self.substitute_payload(item, plugin_name, reveal_sensitive=reveal_sensitive) for item in payload]
        return payload


__all__ = [
    "SENSITIVE_PLACEHOLDER",
    "OptionSchemaField",
    "PluginOptionsStorage",
    "parse_user_config_schema",
]
