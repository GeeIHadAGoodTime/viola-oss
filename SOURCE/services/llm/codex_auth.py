"""Bridge between Codex CLI auth (~/.codex/auth.json) and the codex-auth library.

Lets Viola use a ChatGPT Plus/Pro subscription for LLM calls instead of
per-token API billing. Token refresh, URL rewriting, auth headers, Responses
normalization, and SSE buffering are all delegated to ``codex-auth``'s
``AsyncCodexTransport``.

Four monkey-patches fix codex-auth v0.1.1 bugs at import time:

1. ``_extract_sse_response`` — accumulate output from ``response.output_item.done``
2. ``_chat_completions_to_responses`` — fail closed if a legacy chat caller appears
3. ``_normalize_responses_body`` — strip unsupported sampling params
4. ``_responses_to_chat_completion`` — fail closed if a legacy chat caller appears
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.platform import get_data_dir

logger = logging.getLogger(__name__)

# Codex CLI stores its auth here (written by ``codex login``).
_CODEX_CLI_AUTH = Path.home() / ".codex" / "auth.json"
_CODEX_CLI_MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"
_CODEX_AUTH_STORE = get_data_dir() / "codex_auth.json"

# Default model when using Codex subscription. Must be available to ALL Codex
# users — gpt-5.3-codex-spark is ChatGPT max-plan-only, so it is a selectable
# option (below) but never the default.
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"
CODEX_AUTH_INSTRUCTIONS = "Run codex login in your terminal then click Refresh."
CODEX_AUTH_EXPIRED_INSTRUCTIONS = "Your Codex sign-in expired. Run codex login in your terminal then click Refresh."


def _get_codex_reasoning_param(model: str) -> dict[str, str] | None:
    """Return the Codex reasoning block for reasoning-capable models."""
    _model_lower = model.lower()
    if not any(tag in _model_lower for tag in ("o1", "o3", "o4-", "gpt-5")):
        return None

    from config.defaults import (
        DEFAULT_CODEX_REASONING_EFFORT,
        get_configured_reasoning_effort,
    )

    effort = get_configured_reasoning_effort(
        "codex_reasoning_effort",
        DEFAULT_CODEX_REASONING_EFFORT,
        model,
    )
    return {"effort": effort, "summary": "auto"}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_codex_available() -> bool:
    """Return True when the user has a valid Codex CLI login on disk."""
    try:
        return bool(get_auth_status()["signed_in"])
    except Exception:
        logger.debug("Codex auth check failed", exc_info=True)
        return False


def get_auth_status(*, now: float | None = None) -> dict[str, Any]:
    """Return UI-safe Codex CLI authentication status.

    Reads ``~/.codex/auth.json`` directly because the Codex CLI's
    ``login status`` command is text-only and does not expose the structured
    fields the Settings UI needs.
    """

    now_ts = time.time() if now is None else now
    status: dict[str, Any] = {
        "signed_in": False,
        "expires_at": None,
        "account_email": None,
        "instructions": CODEX_AUTH_INSTRUCTIONS,
        "last_refresh": None,
    }

    data = _read_codex_auth()
    if not isinstance(data, dict):
        return status

    last_refresh = data.get("last_refresh")
    if isinstance(last_refresh, str) and last_refresh.strip():
        status["last_refresh"] = last_refresh.strip()

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return status

    access_token = _clean_string(tokens.get("access_token"))
    if not access_token:
        return status

    access_claims = _decode_jwt_claims(access_token) or {}
    id_claims = _decode_jwt_claims(_clean_string(tokens.get("id_token"))) or {}
    status["account_email"] = _extract_account_email(access_claims, id_claims)

    expires_at_ts = _coerce_timestamp(access_claims.get("exp"))
    if expires_at_ts is None:
        expires_at_ts = _coerce_timestamp(id_claims.get("exp"))
    if expires_at_ts is not None:
        status["expires_at"] = _format_unix_timestamp(expires_at_ts)
        if expires_at_ts <= now_ts:
            status["instructions"] = CODEX_AUTH_EXPIRED_INSTRUCTIONS
            return status

    status["signed_in"] = True
    status["instructions"] = ""
    return status


def launch_codex_login() -> dict[str, Any]:
    """Launch ``codex login`` as a detached process for the current OS."""

    status = get_auth_status()
    if status["signed_in"]:
        return {
            "launched": False,
            "already_signed_in": True,
            "fallback_instructions": None,
            "error": None,
            "status": status,
        }

    executable = _find_codex_executable()
    if executable is None:
        return {
            "launched": False,
            "already_signed_in": False,
            "fallback_instructions": CODEX_AUTH_INSTRUCTIONS,
            "error": "Codex CLI was not found on PATH. Install Codex, or run codex login in your terminal.",
            "status": status,
        }

    command = [str(executable), "login"]
    try:
        popen_kwargs: dict[str, Any] = {"cwd": str(Path.home())}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs.update(
                {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    "start_new_session": True,
                }
            )
        process = subprocess.Popen(command, shell=False, **popen_kwargs)
    except OSError as exc:
        logger.warning("Failed to launch codex login: %s", exc)
        return {
            "launched": False,
            "already_signed_in": False,
            "fallback_instructions": CODEX_AUTH_INSTRUCTIONS,
            "error": "Could not start Codex CLI: %s" % exc,
            "status": status,
        }

    return {
        "launched": True,
        "already_signed_in": False,
        "fallback_instructions": CODEX_AUTH_INSTRUCTIONS,
        "error": None,
        "pid": process.pid,
        "status": status,
    }


def sign_out_codex_auth() -> dict[str, Any]:
    """Delete the Codex CLI auth file if it exists."""

    try:
        existed = _CODEX_CLI_AUTH.exists()
        if existed:
            _CODEX_CLI_AUTH.unlink()
        return {
            "signed_out": True,
            "deleted": existed,
            "error": None,
            "status": get_auth_status(),
        }
    except OSError as exc:
        logger.warning("Failed to delete Codex auth file: %s", exc)
        return {
            "signed_out": False,
            "deleted": False,
            "error": "Could not delete Codex auth file: %s" % exc,
            "status": get_auth_status(),
        }


def get_codex_info() -> dict[str, Any]:
    """Return user-facing info about the Codex subscription.

    Returns a dict with keys: available, email, plan, expires_at, models.
    """
    info: dict[str, Any] = {
        "available": False,
        "email": None,
        "plan": None,
        "expires_at": None,
        "models": [],
    }
    data = _read_codex_auth()
    if data is None:
        return info

    status = get_auth_status()
    if not status["signed_in"]:
        info["email"] = status.get("account_email")
        info["expires_at"] = status.get("expires_at")
        return info

    tokens = data.get("tokens") or {}
    access = tokens.get("access_token", "")
    if not access:
        return info

    info["available"] = True

    # Decode JWT claims (no verification -- we just need display info).
    claims = _decode_jwt_claims(access)
    if claims:
        nested = claims.get("https://api.openai.com/auth") or {}
        info["plan"] = nested.get("chatgpt_plan_type")
        info["expires_at"] = claims.get("exp")  # Unix timestamp

        profile = claims.get("https://api.openai.com/profile") or {}
        info["email"] = profile.get("email") or claims.get("email")

    # Available models from cache.
    info["models"] = get_codex_models()
    return info


def get_codex_models() -> list[str]:
    """Return model slugs available on the Codex subscription."""
    try:
        raw = _CODEX_CLI_MODELS_CACHE.read_text(encoding="utf-8")
        data = json.loads(raw)
        models = data.get("models") or data.get("data") or []
        return [m.get("slug") or m.get("id", "") for m in models if isinstance(m, dict)]
    except Exception:
        # Sensible fallback -- keep the subscription transport model first.
        return [DEFAULT_CODEX_MODEL, "gpt-5.4", "gpt-5.3-codex-spark", "codex-auto-review"]


def create_codex_openai_client() -> Any:
    """Create an ``AsyncOpenAI`` wired to the Codex subscription endpoint.

    Uses ``codex-auth``'s ``AsyncCodexTransport`` directly (handles URL
    rewriting, auth headers, token refresh, Responses normalization, and SSE
    buffering) with monkey-patches applied via ``_apply_codex_patches()`` to fix
    known codex-auth v0.1.1 bugs and reject legacy chat requests.
    """
    data = _read_codex_auth()
    if data is None:
        raise RuntimeError("Codex CLI auth not found. Run `codex login` in a terminal first.")

    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    account_id = tokens.get("account_id", "")

    if not access_token:
        raise RuntimeError(
            "Codex CLI auth file exists but has no access token. " "Run `codex login` to re-authenticate."
        )

    # Suppress codex-auth's global monkey-patch BEFORE importing.
    os.environ["CODEX_AUTH_NO_PATCH"] = "1"

    # Fix codex-auth bugs before creating the transport.
    _apply_codex_patches()

    from codex_auth.patch import AsyncCodexTransport
    from codex_auth.tokens import AuthTokens, TokenStore

    auth = AuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
    )

    store = TokenStore(auth_file=_CODEX_AUTH_STORE)
    try:
        store.save(auth)
    except Exception:
        logger.debug("Could not persist codex tokens to codex-auth store", exc_info=True)

    import httpx
    from openai import AsyncOpenAI

    transport = AsyncCodexTransport(auth_tokens=auth, token_store=store)
    http_client = httpx.AsyncClient(transport=transport)

    return AsyncOpenAI(
        api_key="codex-auth-placeholder",  # pragma: allowlist secret
        # Keep the SDK path at /v1/responses so AsyncCodexTransport can rewrite
        # it to ChatGPT. Env OPENAI_BASE_URL may point at Viola's API gateway.
        base_url="https://api.openai.com/v1",
        http_client=http_client,
    )


def is_codex_token_expiring_soon(hours: int = 24) -> bool:
    """Check whether the Codex token expires within *hours*."""
    data = _read_codex_auth()
    if data is None:
        return True
    tokens = data.get("tokens") or {}
    claims = _decode_jwt_claims(tokens.get("access_token", ""))
    if not claims or "exp" not in claims:
        return True
    return claims["exp"] - time.time() < hours * 3600


# ---------------------------------------------------------------------------
# Monkey-patches for codex-auth bugs
# ---------------------------------------------------------------------------

# Params the Codex endpoint rejects (empirically verified 2026-04-08).
_CODEX_STRIP_PARAMS = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "max_output_tokens",
        "frequency_penalty",
        "presence_penalty",
    }
)

_patches_applied = False


def _fixed_extract_sse_response(raw: bytes) -> dict | None:
    """Parse SSE bytes, accumulate output from ``response.output_item.done``.

    codex-auth's original only reads ``response.completed`` which the Codex
    backend returns with ``output: []``.  This version collects items from
    ``response.output_item.done`` events and injects them into the completed
    response.
    """
    output_items: list[dict] = []
    output_text_parts: list[str] = []
    completed_response: dict | None = None

    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        if etype == "response.output_item.done":
            item = event.get("item")
            if item:
                output_items.append(item)
        elif etype == "response.output_text.done":
            text = event.get("text", "")
            if text:
                output_text_parts.append(text)
        elif etype == "response.completed":
            completed_response = event.get("response")
        elif etype in ("response.failed", "response.incomplete"):
            # Capture failed/incomplete as fallback if no completed event
            if completed_response is None:
                completed_response = event.get("response")

    # If no completed/failed/incomplete but we accumulated output, synthesize
    if completed_response is None:
        if output_items or output_text_parts:
            completed_response = {
                "id": "",
                "status": "completed",
                "output": output_items,
                "output_text": "\n".join(output_text_parts),
                "model": "",
                "usage": {},
            }

        if completed_response is None:
            return None

    if output_items:
        completed_response["output"] = output_items
    if output_text_parts and not completed_response.get("output_text"):
        completed_response["output_text"] = "\n".join(output_text_parts)

    return completed_response


def _reject_chat_completions_to_responses(_body: dict) -> dict:
    """Fail closed if a caller tries to use legacy chat through Codex auth."""
    raise RuntimeError("Codex provider accepts Responses API requests only")


def _fixed_normalize_responses_body(body: dict) -> dict:
    """Normalize a Responses API body for the Codex endpoint.

    Same as codex-auth's original but also strips sampling params the
    endpoint rejects and ensures reasoning models include the reasoning
    parameter.
    """
    body = body.copy()
    if isinstance(body.get("input"), str):
        body["input"] = [{"role": "user", "content": body["input"]}]
    body.setdefault("instructions", "You are a helpful assistant.")
    body["store"] = False  # Codex endpoint requires store=false; overrides caller's store=true
    body["stream"] = True
    for param in _CODEX_STRIP_PARAMS:
        body.pop(param, None)
    # Ensure reasoning models have the reasoning param (Codex endpoint
    # requires it; without it, returns 401 missing_scope).
    model = body.get("model", "")
    reasoning = _get_codex_reasoning_param(model)
    if "reasoning" not in body and reasoning is not None:
        body["reasoning"] = reasoning
    return body


def _reject_responses_to_chat_completion(_resp: dict) -> dict:
    """Fail closed if codex-auth tries to back-convert a Responses payload."""
    raise RuntimeError("Codex provider does not expose Chat Completions responses")


def _apply_codex_patches() -> None:
    """Monkey-patch codex-auth's broken functions."""
    global _patches_applied
    if _patches_applied:
        return

    import codex_auth.patch as patch_mod

    # 1. SSE accumulation: _extract_sse_response only reads response.completed
    #    (empty output); fix accumulates from response.output_item.done.
    patch_mod._extract_sse_response = _fixed_extract_sse_response

    # 2. Fail closed if any caller attempts the legacy chat endpoint.
    patch_mod._chat_completions_to_responses = _reject_chat_completions_to_responses

    # 3. Responses body normalizer doesn't strip unsupported params.
    patch_mod._normalize_responses_body = _fixed_normalize_responses_body

    # 4. Fail closed rather than back-converting Responses into chat shape.
    patch_mod._responses_to_chat_completion = _reject_responses_to_chat_completion

    _patches_applied = True
    logger.debug("Applied codex-auth patches (SSE accumulation, param stripping)")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_codex_auth() -> dict[str, Any] | None:
    """Read and parse ``~/.codex/auth.json``."""
    try:
        raw = _CODEX_CLI_AUTH.read_text(encoding="utf-8")
        return json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:
        logger.debug("Failed to read Codex auth file", exc_info=True)
        return None


def _decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode JWT payload without signature verification."""
    import base64

    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _clean_string(value: object) -> str:
    """Return a stripped string or ``""`` for non-string values."""

    return value.strip() if isinstance(value, str) else ""


def _coerce_timestamp(value: object) -> float | None:
    """Coerce a JWT timestamp claim to a Unix timestamp."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _format_unix_timestamp(timestamp: float) -> str:
    """Format a Unix timestamp as a compact UTC ISO string."""

    return datetime.fromtimestamp(timestamp, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_account_email(access_claims: dict[str, Any], id_claims: dict[str, Any]) -> str | None:
    """Extract the account email from known Codex/OpenAI JWT claim locations."""

    profile = access_claims.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        email = _clean_string(profile.get("email"))
        if email:
            return email

    for claims in (access_claims, id_claims):
        email = _clean_string(claims.get("email"))
        if email:
            return email

    return None


def _find_codex_executable() -> Path | None:
    """Resolve the Codex CLI launcher without selecting PowerShell's ps1 shim."""

    candidates: list[str | Path | None] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "codex.cmd")
        candidates.extend([shutil.which("codex.cmd"), shutil.which("codex")])
    else:
        candidates.extend(
            [
                shutil.which("codex"),
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/local/bin/codex"),
                Path.home() / ".local" / "bin" / "codex",
            ]
        )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        path = Path(candidate)
        normalized = path.resolve() if path.exists() else path
        if normalized in seen:
            continue
        seen.add(normalized)
        if path.exists() and path.is_file():
            return path
    return None
