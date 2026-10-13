"""Synthetic behavior proof for the Ollama fallback's BYOK model boundary."""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def _load_fallback_method():
    """Execute the complete production method without initializing app/device state."""
    path = ROOT / "services/llm/provider_router.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ProviderAgnosticRouter")
    method = next(
        node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_try_create_fallback"
    )
    namespace = {"logger": logging.getLogger(__name__), "OLLAMA_DEFAULT_BASE_URL": "http://127.0.0.1:11434"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(path), "exec"), namespace)
    return namespace["_try_create_fallback"]


class OllamaOpenAIFallbackModelTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("config.defaults", ROOT / "config/defaults.py")
        self.defaults = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.defaults)
        self.fallback = _load_fallback_method()
        self.provider = Mock()
        self.provider.is_available.return_value = True
        self.factory = types.ModuleType("services.llm.factory")
        self.factory.create_llm_provider = Mock(return_value=self.provider)
        base = types.ModuleType("services.llm.providers.base")
        base.LLMConfig = SimpleNamespace
        self.modules = patch.dict(
            sys.modules,
            {
                "config.defaults": self.defaults,
                "services.llm.factory": self.factory,
                "services.llm.providers.base": base,
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def route(self, *, model="", api_key="synthetic-key", strategy="auto"):
        settings = Mock()
        values = {
            "llm_provider": "ollama",
            "llm_api_key": api_key,
            "llm_model": model,
            "llm_fallback_strategy": strategy,
            "ai_source": "codex",
        }
        settings.get.side_effect = lambda key, default=None: values.get(key, default)
        router = SimpleNamespace(_fallback_provider=None, _haiku_provider=None)
        self.fallback(router, settings)
        return router

    def test_blank_model_uses_byok_default_even_when_managed_default_changes(self):
        self.defaults.DEFAULT_MANAGED_MODEL = "managed-only-model"
        router = self.route()
        config = self.factory.create_llm_provider.call_args.args[0]
        self.assertIs(router._fallback_provider, self.provider)
        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.api_key, "synthetic-key")
        self.assertEqual(config.model, self.defaults.DEFAULT_GPT_MODEL)
        self.assertNotEqual(config.model, self.defaults.DEFAULT_MANAGED_MODEL)

    def test_explicit_historical_byok_model_is_not_upgraded_to_managed(self):
        self.route(model="gpt-5.4-mini")
        self.assertEqual(self.factory.create_llm_provider.call_args.args[0].model, "gpt-5.4-mini")

    def test_explicit_custom_model_is_preserved(self):
        self.route(model="byok-selected-model")
        self.assertEqual(self.factory.create_llm_provider.call_args.args[0].model, "byok-selected-model")

    def test_missing_or_encrypted_placeholder_key_never_constructs_provider(self):
        for key in ("", "***ENCRYPTED***"):
            with self.subTest(key=key):
                self.assertIsNone(self.route(api_key=key)._fallback_provider)
        self.factory.create_llm_provider.assert_not_called()

    def test_disabled_fallback_strategies_never_construct_provider(self):
        for strategy in ("local_only", "cloud_only"):
            with self.subTest(strategy=strategy):
                self.assertIsNone(self.route(strategy=strategy)._fallback_provider)
        self.factory.create_llm_provider.assert_not_called()

    def test_unavailable_provider_is_not_retained(self):
        self.provider.is_available.return_value = False
        self.assertIsNone(self.route()._fallback_provider)


if __name__ == "__main__":
    unittest.main()
