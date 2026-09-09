"""Utilities for local LLM model discovery and selection."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from core.constants import TIMEOUT_DEFAULT, TIMEOUT_LONG
from core.logging_config import get_logger

# NOTE: `scripts.proc_tree` is imported LAZILY inside list_ollama_models_from_cli
# (the only caller), NOT at module scope. factory.py imports this module at its own
# module top level, so a module-scope `from scripts import proc_tree` made the WHOLE
# LLM provider router fail to import whenever proc_tree.py was absent from the curated
# cloud image -- every /api/v1/command then fell back to "No handler matched" (the
# 2026-07-17 prod outage, #2243; recurrence of #362/#2231). Keeping the import lazy
# means factory.py imports clean regardless of whether proc_tree.py shipped, and the
# one runtime path that needs it degrades gracefully (returns [] Ollama models) if it
# is genuinely missing, instead of taking cloud AI down.

logger = get_logger(__name__)


def looks_like_cloud_model(model: str) -> bool:
    """Return True for model IDs that should not be sent to local providers."""
    normalized = (model or "").strip().lower()
    return normalized.startswith(("gpt-", "o1-", "o3-", "o4-", "claude", "gemini"))


def local_model_sort_key(name: str) -> tuple[int, float, int]:
    """Rank local models for default selection without provider-specific metadata."""
    normalized = (name or "").strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*b", normalized)
    params_b = float(match.group(1)) if match else 0.0
    context_bonus = 1 if "32k" in normalized or "32768" in normalized else 0
    return (
        int("viola" in normalized) + int("qwen" in normalized) + int("coder" in normalized) + context_bonus,
        params_b,
        len(normalized),
    )


def sort_local_models(models: list[str]) -> list[str]:
    """Return unique local model IDs with the best Viola default first."""
    seen: set[str] = set()
    unique: list[str] = []
    for model in models:
        normalized = (model or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return sorted(unique, key=local_model_sort_key, reverse=True)


def best_local_model(models: list[str]) -> str:
    """Return the best local model candidate, or an empty string."""
    ranked = sort_local_models(models)
    return ranked[0] if ranked else ""


def ollama_model_matches(candidate: str, installed: str) -> bool:
    """Match exact Ollama tags and untagged aliases."""
    candidate_norm = (candidate or "").strip().lower()
    installed_norm = (installed or "").strip().lower()
    if not candidate_norm or not installed_norm:
        return False
    return candidate_norm == installed_norm or candidate_norm == installed_norm.split(":", 1)[0]


def extract_local_ai_models(server_type: str, payload: Any) -> list[str]:
    """Extract model names from a local AI discovery response."""
    if not isinstance(payload, dict):
        return []

    if server_type == "ollama":
        models = payload.get("models", [])
        if not isinstance(models, list):
            return []
        return sort_local_models(
            [
                name
                for item in models
                if isinstance(item, dict)
                for name in (item.get("name") or item.get("model"),)
                if isinstance(name, str) and name
            ]
        )

    data = payload.get("data", payload.get("models", []))
    if not isinstance(data, list):
        return []
    return sort_local_models(
        [
            model_id
            for item in data
            if isinstance(item, (dict, str))
            for model_id in ((item.get("id") or item.get("name")) if isinstance(item, dict) else item,)
            if isinstance(model_id, str) and model_id
        ]
    )


def _ollama_executable_candidates() -> list[Path]:
    candidates: list[Path] = []
    from_path = shutil.which("ollama")
    if from_path:
        candidates.append(Path(from_path))

    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe",
            Path("C:/Program Files/Ollama/ollama.exe"),
            Path("C:/Program Files (x86)/Ollama/ollama.exe"),
        ]
    )
    return candidates


def find_ollama_executable() -> Path | None:
    """Find an installed Ollama CLI without requiring it to be on PATH."""
    for candidate in _ollama_executable_candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def parse_ollama_list_output(output: str) -> list[str]:
    """Parse `ollama list` output into model names."""
    models: list[str] = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        name = stripped.split(None, 1)[0].strip()
        if name:
            models.append(name)
    return sort_local_models(models)


def list_ollama_models_from_cli(timeout_seconds: float = 8.0) -> list[str]:
    """List installed Ollama models using the local CLI as a disk-backed fallback."""
    executable = find_ollama_executable()
    if executable is None:
        return []
    try:
        # Lazy import (#2243): keep `scripts.proc_tree` out of module scope so
        # factory.py -> local_models imports clean even if proc_tree.py is absent
        # from the curated cloud image. If it is genuinely missing, the ImportError
        # is caught by the broad `except` below and Ollama discovery returns [] --
        # graceful degradation, never a router-killing import failure.
        from scripts import proc_tree

        # ollama is a substantive external CLI (not a tiny leaf binary), so this is
        # routed through proc_tree.run's tree-killing runner rather than raw
        # subprocess.run. Default raise_on_timeout=False is fine: a timeout already
        # falls through to the `result.returncode != 0` branch below, returning []
        # the same as the broad except clause here would on a raised TimeoutExpired.
        result = proc_tree.run(
            [str(executable), "list"],
            timeout=timeout_seconds,
        )
    except Exception as exc:
        logger.debug("Ollama CLI model discovery failed: %s", exc)
        return []
    if result.returncode != 0:
        logger.debug("Ollama CLI list returned %s: %s", result.returncode, result.stderr.strip())
        return []
    return parse_ollama_list_output(result.stdout)


async def _probe_local_ai_server(
    client: Any,
    *,
    server_type: str,
    name: str,
    url: str,
    endpoint: str,
) -> dict[str, Any] | None:
    """Probe one local AI server and return normalized server metadata."""
    try:
        response = await client.get(endpoint)
        if response.status_code != 200:
            return None
        models = extract_local_ai_models(server_type, response.json())
        return {
            "type": server_type,
            "url": url,
            "models": models,
            "name": name,
            "running": True,
            "detected_from": "server",
        }
    except Exception:
        return None


def _merge_ollama_cli_fallback(servers: list[dict[str, Any]], models: list[str]) -> list[dict[str, Any]]:
    if not models:
        return servers

    for server in servers:
        if server.get("type") == "ollama":
            existing = server.get("models")
            if not isinstance(existing, list) or not existing:
                server["models"] = models
                server["detected_from"] = "server+cli"
            return servers

    return [
        {
            "type": "ollama",
            "name": "Ollama",
            "url": "http://localhost:11434",
            "models": models,
            "running": False,
            "detected_from": "ollama_cli",
        },
        *servers,
    ]


async def detect_local_ai_servers() -> list[dict[str, Any]]:
    """Detect local AI servers and installed local models without blocking the event loop."""
    import httpx

    probes = (
        {
            "type": "ollama",
            "name": "Ollama",
            "url": "http://localhost:11434",
            "endpoint": "http://localhost:11434/api/tags",
        },
        {
            "type": "openai_compatible",
            "name": "LM Studio",
            "url": "http://localhost:1234/v1",
            "endpoint": "http://localhost:1234/v1/models",
        },
        {
            "type": "openai_compatible",
            "name": "vLLM",
            "url": "http://localhost:8000/v1",
            "endpoint": "http://localhost:8000/v1/models",
        },
        {
            "type": "openai_compatible",
            "name": "llama.cpp server / LocalAI",
            "url": "http://localhost:8080/v1",
            "endpoint": "http://localhost:8080/v1/models",
        },
        {
            "type": "openai_compatible",
            "name": "text-generation-webui",
            "url": "http://localhost:5000/v1",
            "endpoint": "http://localhost:5000/v1/models",
        },
    )
    timeout = httpx.Timeout(TIMEOUT_LONG, connect=TIMEOUT_DEFAULT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            *(
                _probe_local_ai_server(
                    client,
                    server_type=probe["type"],
                    name=probe["name"],
                    url=probe["url"],
                    endpoint=probe["endpoint"],
                )
                for probe in probes
            ),
            return_exceptions=True,
        )

    servers = [result for result in results if isinstance(result, dict)]
    ollama_cli_models = await asyncio.to_thread(list_ollama_models_from_cli)
    return _merge_ollama_cli_fallback(servers, ollama_cli_models)
