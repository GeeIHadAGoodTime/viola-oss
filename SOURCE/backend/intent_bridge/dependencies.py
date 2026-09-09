from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from core.logging_config import get_logger

from .config import IntentBridgeConfig

logger = get_logger(__name__)


class _SettingsManager(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def set(self, key: str, value: object, *, save_immediately: bool = True) -> bool: ...


class _ConversationStoreFactory(Protocol):
    def __call__(self, *, device_id: str | None) -> object: ...


class _ConversationSyncBridge(Protocol):
    def load_conversation_history(self) -> None: ...


class _WrapConversationSync(Protocol):
    def __call__(self, handler: object, store: object, *, auto_save: bool) -> _ConversationSyncBridge: ...


@dataclass(frozen=True)
class InterpreterArtifacts:
    kind: str | None
    interpreter_cls: type[object] | None
    interpret_func: Callable[..., object] | None
    dispatch_func: Callable[..., object] | None


@dataclass(frozen=True)
class GptArtifacts:
    handler_cls: Callable[..., object] | None
    conversation_store_cls: _ConversationStoreFactory | None
    wrap_sync_func: _WrapConversationSync | None


class DependencyResolver:
    """Centralizes dynamic imports to keep the bridge itself lightweight."""

    def __init__(self, settings: object, config: IntentBridgeConfig) -> None:
        self._settings = settings
        self._config = config

    @property
    def settings(self) -> object:
        return self._settings

    # ------------------------------------------------------------------ helpers
    def resolve_interpreter(self) -> InterpreterArtifacts:
        kind, interpreter_cls, interpret_func, dispatch_func = self._try_import_interpreter_symbols()
        return InterpreterArtifacts(
            kind=kind,
            interpreter_cls=interpreter_cls,
            interpret_func=interpret_func,
            dispatch_func=dispatch_func,
        )

    def resolve_gpt(self) -> GptArtifacts:
        if not self._config.enable_gpt:
            logger.info("IntentBridge GPT integration disabled via config.")
            return GptArtifacts(None, None, None)

        # Canonical AI path: Try LLM router first
        try:
            from intent.pipeline import create_llm_router

            llm_router = create_llm_router(self._settings)
            if llm_router:
                logger.info("Canonical LLM router available for intent bridge")
                # Return LLM router as GPTPort-compatible handler
                return GptArtifacts(
                    handler_cls=lambda *args, **kwargs: llm_router,  # Return router directly
                    conversation_store_cls=None,  # LLM router handles its own persistence
                    wrap_sync_func=None,  # LLM router handles sync internally
                )
        except Exception as exc:
            logger.debug("LLM router not available: %s", exc)

        # No legacy fallback - single canonical AI path enforced
        # Legacy GPT stack has been removed. If LLM router is unavailable,
        # the system should fail gracefully rather than fall back to deprecated handlers.
        logger.warning(
            "LLM router unavailable and no fallback configured. "
            "Single canonical AI path policy: no legacy GPT handlers allowed."
        )
        handler_cls = None
        conversation_store_cls = None
        wrap_sync = None

        return GptArtifacts(
            handler_cls=handler_cls,
            conversation_store_cls=conversation_store_cls,
            wrap_sync_func=wrap_sync,
        )

    def resolve_skill_manager(
        self,
        music: object | None,
        tts: object | None,
        state: object | None,
        enable_skills: bool,
    ) -> object | None:
        if not enable_skills:
            logger.info("IntentBridge skill system disabled via config.")
            return None

        try:
            from skills.base import SkillContext
            from skills.manager import SkillManager
        except Exception as exc:
            logger.warning("Skill system unavailable: %s", exc)
            return None

        context = SkillContext(
            music_player=music,
            tts_engine=tts,
            gpt_handler=None,
            settings=self._settings,
            state=state,
        )

        try:
            manager = SkillManager(context)
            manager.discover_and_load()
        except Exception as exc:
            logger.warning("Skill system initialization failed: %s", exc)
            return None

        try:
            stats: dict[str, object] = getattr(manager, "get_stats", lambda: {})()
            logger.info(
                "Skill system initialized: %s skills loaded",
                stats.get("total_skills", "unknown"),
            )
        except Exception:
            logger.info("Skill system initialized")

        return manager

    def instantiate_gpt_handler(
        self,
        artifacts: GptArtifacts,
        music_player: object | None,
        state: object | None,
    ) -> object | None:
        handler_cls = artifacts.handler_cls
        if handler_cls is None:
            return None

        try:
            handler = handler_cls(self._settings, music_player=music_player, app_state=state)
        except Exception as exc:
            logger.exception("Failed to instantiate GptHandler: %s", exc)
            return None

        logger.info("GptHandler instance created")
        return handler

    def ensure_conversation_sync(
        self,
        artifacts: GptArtifacts,
        gpt_handler: object | None,
    ) -> None:
        if gpt_handler is None:
            return
        if artifacts.conversation_store_cls is None or artifacts.wrap_sync_func is None:
            return

        try:
            conversation_store = artifacts.conversation_store_cls(device_id=self._resolve_device_id())
            sync_bridge = artifacts.wrap_sync_func(gpt_handler, conversation_store, auto_save=True)
            try:
                # mt-ok: store constructed with device_id; sync_bridge instance carries it
                sync_bridge.load_conversation_history()
            except Exception as exc:
                logger.debug("Could not load conversation history: %s", exc)
            logger.info("GptHandler wrapped with conversation sync")
        except Exception as exc:
            logger.debug("Conversation sync integration failed: %s", exc)

    # ------------------------------------------------------------- private impl
    @staticmethod
    def _try_import_interpreter_symbols() -> tuple[
        str | None,
        type[object] | None,
        Callable[..., object] | None,
        Callable[..., object] | None,
    ]:
        try:
            from intent.interpreter import IntentInterpreter

            return ("class", IntentInterpreter, None, None)
        except Exception as exc:
            logger.warning("Intent interpreter unavailable: %s", exc)
            return (None, None, None, None)

    @staticmethod
    def _resolve_device_id() -> str | None:
        try:
            from ui.settings_manager import get_settings_manager
        except Exception:
            logger.exception("settings_manager import failed")
            return None

        try:
            settings_mgr = get_settings_manager()
        except Exception:
            logger.exception("get_settings_manager() failed")
            return None

        typed_mgr = cast(_SettingsManager, settings_mgr)
        device_id: str | None = None
        try:
            raw = typed_mgr.get("device_id", "")
            if isinstance(raw, str) and raw:
                device_id = raw
        except Exception:
            device_id = None

        if not device_id:
            device_id = str(uuid.uuid4())
            try:
                typed_mgr.set("device_id", device_id, save_immediately=False)
            except Exception as exc:
                logger.warning(
                    "Unable to persist generated device_id; keeping in-memory copy only: %s",
                    exc,
                )
            else:
                logger.debug("Generated device_id persisted (prefix=%s...)", device_id[:8])

        return device_id
