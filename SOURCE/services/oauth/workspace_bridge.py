"""Bridge Viola's Google external-service OAuth tokens to the Google Workspace MCP server.

When a user authenticates with Google via Viola's OAuth flow, this module
exports their tokens to the Workspace MCP server's credential storage so
the MCP server can access Gmail, Drive, Docs, Sheets, Calendar, etc.
without a separate login.

The bridge also provides a token refresh endpoint that the MCP server
calls when its access token expires (replacing the default cloud function).
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from core.logging_config import get_logger
from core.subprocess_utils import run_silent

logger = get_logger(__name__)

WORKSPACE_CREDENTIAL_FILES = (
    "gemini-cli-workspace-token.json",
    ".gemini-cli-workspace-master-key",
)
_WORKSPACE_CACHE_OWNER_FILE = ".viola-workspace-cache-owner"


def _workspace_cache_is_local_to_user(user_id: str) -> bool:
    """Whether the fixed Workspace cache belongs to this desktop user.

    The external Workspace MCP implementation stores credentials in one fixed
    file under its project root.  It has no per-user storage-root or transport
    binding, so it is safe only for the one active user of a desktop install.
    ``get_desktop_local_principals`` includes both its signed-in account id and
    device id and raises on the multi-tenant cloud surface.
    """
    try:
        from core.user_context import get_desktop_local_principals

        return user_id in get_desktop_local_principals()
    except (ImportError, LookupError, OSError, RuntimeError, ValueError):
        return False


def _principal_fingerprint(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _active_desktop_principal_fingerprints() -> set[str]:
    try:
        from core.user_context import get_desktop_local_principals

        return {_principal_fingerprint(principal) for principal in get_desktop_local_principals() if principal}
    except (ImportError, LookupError, OSError, RuntimeError, ValueError):
        return set()


def _cache_owner_path(root: Path) -> Path:
    return root / _WORKSPACE_CACHE_OWNER_FILE


def _read_cache_owner(root: Path) -> str | None:
    try:
        value = _cache_owner_path(root).read_text(encoding="ascii").strip()
        return value or None
    except OSError:
        return None


def _write_cache_owner(root: Path, user_id: str) -> None:
    _cache_owner_path(root).write_text(_principal_fingerprint(user_id), encoding="ascii")


def _cache_has_credentials(root: Path) -> bool:
    return any((root / filename).exists() for filename in WORKSPACE_CREDENTIAL_FILES)


def _remove_workspace_cache(root: Path, *, include_owner_marker: bool = True) -> bool:
    filenames = (*WORKSPACE_CREDENTIAL_FILES, _WORKSPACE_CACHE_OWNER_FILE) if include_owner_marker else WORKSPACE_CREDENTIAL_FILES
    for filename in filenames:
        path = root / filename
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Failed to clear Workspace MCP credential cache file %s: %s", path, exc)
            return False
    return True


def _retire_cache_for_new_desktop_owner(root: Path) -> bool:
    """Remove an old or legacy cache before another desktop account can use it."""
    if not _cache_has_credentials(root):
        return True
    owner = _read_cache_owner(root)
    if owner is not None and owner in _active_desktop_principal_fingerprints():
        return True
    # This is an account-handoff cleanup, not user-initiated revocation. The
    # former owner is no longer an active principal on this one-user desktop.
    return _remove_workspace_cache(root)


def _get_workspace_mcp_root() -> Path | None:
    """Resolve the Workspace MCP server project root from the configured path."""
    from config.settings import get_settings

    mcp_path = getattr(get_settings(), "google_workspace_mcp_path", "")
    if not mcp_path:
        return None
    # The setting points to workspace-server/dist/index.js — go up to project root.
    p = Path(mcp_path).resolve()
    # workspace-server/dist/index.js -> workspace-server/dist -> workspace-server -> project root
    root = p.parent.parent.parent
    if (root / "gemini-extension.json").exists():
        return root
    return None


def clear_exported_workspace_tokens(user_id: str) -> bool:
    """Delete exported Google credentials from the Workspace MCP cache.

    Returns True when there is no configured cache or every cache file was
    absent/removed. Returns False when an on-disk credential file could not be
    removed so callers can fail the user-visible revocation.
    """
    root = _get_workspace_mcp_root()
    if not root:
        return True
    if not _workspace_cache_is_local_to_user(user_id):
        logger.warning("Refusing to clear a shared Workspace credential cache outside its desktop owner")
        return False

    if _cache_has_credentials(root):
        owner = _read_cache_owner(root)
        if owner is not None and owner != _principal_fingerprint(user_id):
            logger.warning("Refusing to clear Workspace credentials owned by another desktop account")
            return False
        if owner is None:
            logger.warning("Refusing to clear an unbound legacy Workspace credential cache")
            return False

    removed = _remove_workspace_cache(root)
    if removed:
        logger.info("Cleared exported Google tokens from Workspace MCP credential cache")
    return removed


async def export_tokens_for_workspace(user_id: str) -> bool:
    """Export Viola's Google OAuth tokens to the Workspace MCP credential file.

    Writes the user's access/refresh tokens into the MCP server's encrypted
    token storage so the server can make Google API calls without separate auth.

    Returns True on success, False if tokens are unavailable.
    """
    if not _workspace_cache_is_local_to_user(user_id):
        logger.warning("Workspace credential export is unavailable outside the active desktop user")
        return False

    try:
        from services.oauth.credentials import get_google_credentials
        from services.oauth.google import get_enabled_workspace_scopes

        workspace_scopes = get_enabled_workspace_scopes()
        if not workspace_scopes:
            logger.debug("Google Workspace bridge skipped because restricted Google features are disabled")
            return False

        root = _get_workspace_mcp_root()
        if not root:
            logger.debug("Workspace MCP server not configured — bridge skipped")
            return False

        creds = await get_google_credentials(user_id, required_scopes=workspace_scopes)
        if not creds or not creds.token:
            if not _retire_cache_for_new_desktop_owner(root):
                logger.warning("Workspace bridge could not retire stale credential cache")
            logger.info("No Google credentials for user %s — workspace bridge skipped", user_id)
            return False

        scope_set: set[str] = set(creds.scopes or ())
        missing_scopes = sorted(set(workspace_scopes) - scope_set)
        if missing_scopes:
            logger.info(
                "Google credentials for user %s lack Workspace scopes; re-auth required before MCP export",
                user_id,
            )
            return False

        if not _retire_cache_for_new_desktop_owner(root):
            logger.warning("Workspace bridge refused to replace an unretired credential cache")
            return False

        scope_str = " ".join(sorted(scope_set))

        # Build the credential payload in the format the MCP server expects.
        token_data = {
            "main-account": {
                "serverName": "main-account",
                "token": {
                    "accessToken": creds.token,
                    "refreshToken": creds.refresh_token or "",
                    "tokenType": "Bearer",
                    "scope": scope_str,
                    "expiresAt": int(creds.expiry.timestamp() * 1000) if creds.expiry else 0,
                },
                "updatedAt": int(__import__("time").time() * 1000),
            }
        }

        # The MCP server uses Node.js AES-256-GCM encryption with a scrypt-derived key.
        # Rather than reimplementing that in Python, we call a tiny Node script that
        # uses the MCP server's own encryption to write the file.
        _write_via_node(root, token_data)
        _write_cache_owner(root, user_id)
        logger.info("Exported Google tokens to Workspace MCP for user %s", user_id)
        return True

    except Exception:
        logger.exception("Failed to export tokens to Workspace MCP")
        return False


def _write_via_node(mcp_root: Path, token_data: dict) -> None:
    """Use Node.js to write tokens with the MCP server's own encryption.

    Token data is passed over stdin, not CLI args or temp files, to avoid
    process-listing leaks and plaintext token artifacts on disk.
    """
    script = """
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = process.argv[2];
const tokenJson = fs.readFileSync(0, 'utf8');

const tokenPath = path.join(root, 'gemini-cli-workspace-token.json');
const keyPath = path.join(root, '.gemini-cli-workspace-master-key');

// Load or create master key
let masterKey;
try {
    masterKey = fs.readFileSync(keyPath);
} catch (e) {
    if (e.code === 'ENOENT') {
        masterKey = crypto.randomBytes(32);
        fs.writeFileSync(keyPath, masterKey, { mode: 0o600 });
    } else { throw e; }
}

// Derive encryption key (same as file-token-storage.ts)
const salt = `${os.hostname()}-${os.userInfo().username}-gemini-cli-workspace`;
const encKey = crypto.scryptSync(masterKey, salt, 32);

// Encrypt
const iv = crypto.randomBytes(16);
const cipher = crypto.createCipheriv('aes-256-gcm', encKey, iv);
let encrypted = cipher.update(tokenJson, 'utf8', 'hex');
encrypted += cipher.final('hex');
const authTag = cipher.getAuthTag();

const result = iv.toString('hex') + ':' + authTag.toString('hex') + ':' + encrypted;
fs.writeFileSync(tokenPath, result, { mode: 0o600 });
console.log('OK');
"""
    result = run_silent(
        ["node", "-e", script, str(mcp_root)],
        capture_output=True,
        text=True,
        input=json.dumps(token_data),
        timeout=10,
    )  # proc-tree-ok: needs stdin (input=), which proc_tree.run does not support; the
    # inline script above is crypto+fs only (no child_process calls) -- certified non-forking
    if result.returncode != 0:
        raise RuntimeError("Node token export failed: %s" % result.stderr.strip())


async def handle_refresh_token(refresh_token: str) -> dict:
    """Refresh a Google OAuth token using Viola's client credentials.

    This is called by the Workspace MCP server instead of the default
    cloud function. We have the client_secret that the MCP server doesn't.

    Returns dict with access_token, expiry_date, token_type, scope.
    """
    from config.settings import get_settings
    from services.oauth.google import is_google_restricted_features_enabled

    settings = get_settings()
    if not is_google_restricted_features_enabled(settings):
        raise ValueError("Google Workspace restricted features are disabled for this build")

    client_id = getattr(settings, "google_client_id", "")
    client_secret = getattr(settings, "google_client_secret", "")

    if not client_id or not client_secret:
        raise ValueError("Google OAuth client credentials not configured")

    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError("Google token refresh failed (%d): %s" % (resp.status, text))

            data = await resp.json()
            return {
                "access_token": data["access_token"],
                "expiry_date": int(__import__("time").time() * 1000) + data.get("expires_in", 3600) * 1000,
                "token_type": data.get("token_type", "Bearer"),
                "scope": data.get("scope", ""),
            }
