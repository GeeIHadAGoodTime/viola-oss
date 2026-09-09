"""Plugin trust evaluation — fail-closed gate for executing plugin code.

Background (SEC-031 / SEC-034, security sweep 2026-06-09): the plugin loader
``exec_module``s any discovered plugin's entry point with **no** signature or
trust check, and new plugins are enabled-by-default. A prompt-injected agent (or
any local process) that drops a directory under ``plugins/user/`` therefore gets
arbitrary code execution on the next discovery/startup, bypassing the entire MCP
approval system.

The trust model implemented here:

* **Trusted by location** — a plugin whose directory lives under the builtin
  directory ships *with* the installed product and is trusted by virtue of being
  part of the install image (the same trust boundary as the rest of the app
  code). Tampering with the install tree is a separate, higher-privilege threat.

* **Trusted by signature** — any other plugin (e.g. under ``plugins/user/``)
  MUST carry a valid Ed25519 ``signature`` that verifies against one of the
  operator-provisioned trusted public keys (PEM files under the trusted-keys
  directory). This is the marketplace / sideload path.

* **Everything else fails closed** — no valid signature and not built-in ⇒ the
  plugin is NOT loaded and NOT executed. With zero trusted keys provisioned
  (the default today) this means *no* user/sideloaded plugin can execute, which
  is exactly the desired posture until a signing pipeline exists.

This module is the single source of truth for that decision so the loader, the
manager, and the Ratchet gate all agree.
"""

from __future__ import annotations

from pathlib import Path

from core.logging_config import get_logger

from .errors import SIGNATURE_INVALID, PluginError
from .manifest import PluginManifest

logger = get_logger(__name__)

# Trusted public keys live here, one Ed25519 public key per ``*.pem`` file.
# Resolved lazily and relative to the builtin dir's parent (the ``plugins``
# package root) so it stays inside the install image, never a user-writable
# cwd-relative default. Today the directory ships empty → sideloaded plugins
# fail closed.
_TRUSTED_KEYS_DIRNAME = "trusted_keys"


class PluginTrustError(PluginError):
    """Raised when a plugin is neither built-in nor validly signed."""

    def __init__(self, plugin_name: str, message: str) -> None:
        super().__init__(kind=SIGNATURE_INVALID, message=message, plugin_name=plugin_name)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_builtin_location(plugin_path: Path, builtin_dir: Path) -> bool:
    """True when ``plugin_path`` lives under the install's builtin plugin dir."""
    return _is_under(Path(plugin_path), Path(builtin_dir))


def trusted_keys_dir(builtin_dir: Path) -> Path:
    """Directory that holds operator-provisioned trusted public keys (PEM)."""
    # ``plugins/builtin`` → ``plugins/trusted_keys`` (inside the install image).
    return Path(builtin_dir).parent / _TRUSTED_KEYS_DIRNAME


def load_trusted_public_keys(builtin_dir: Path) -> list[str]:
    """Read every ``*.pem`` trusted public key. Missing dir ⇒ empty list."""
    keys_dir = trusted_keys_dir(builtin_dir)
    if not keys_dir.is_dir():
        return []
    keys: list[str] = []
    for pem in sorted(keys_dir.glob("*.pem")):
        try:
            keys.append(pem.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Failed to read trusted plugin key %s: %s", pem, exc)
    return keys


def signature_is_trusted(manifest: PluginManifest, trusted_keys: list[str]) -> bool:
    """True only if the manifest signature verifies against a trusted key."""
    if not manifest.signature or not trusted_keys:
        return False
    for key_pem in trusted_keys:
        try:
            if manifest.verify_signature(key_pem):
                return True
        except (ValueError, TypeError, OSError) as exc:  # pragma: no cover - defensive
            logger.warning("Trusted-key verification error for %s: %s", manifest.name, exc)
    return False


def is_trusted_plugin(plugin_path: Path, manifest: PluginManifest, builtin_dir: Path) -> bool:
    """Return whether this plugin may have its code executed.

    Trusted = shipped under the builtin dir OR carries a signature that verifies
    against a provisioned trusted public key. Fail-closed otherwise.
    """
    if is_builtin_location(plugin_path, builtin_dir):
        return True
    return signature_is_trusted(manifest, load_trusted_public_keys(builtin_dir))


def assert_plugin_trusted(plugin_path: Path, manifest: PluginManifest, builtin_dir: Path) -> None:
    """Raise :class:`PluginTrustError` (fail-closed) for an untrusted plugin."""
    if is_trusted_plugin(plugin_path, manifest, builtin_dir):
        return
    raise PluginTrustError(
        manifest.name,
        "Refusing to load untrusted plugin %s: not a built-in plugin and its "
        "manifest carries no signature trusted by this install. Sideloaded "
        "plugins must be signed by a provisioned trusted key." % manifest.name,
    )
