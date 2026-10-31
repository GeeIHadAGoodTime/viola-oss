"""Kokoro hands ONNX Runtime's CPU arena back after every synthesis.

Without the shrink option the arena keeps the peak of every run for the life of
the session; on the desktop seven ordinary replies grew the process by about
500 MB that never came back (round-7 memory report). The vendored wrapper runs
unchanged here against a recording session and a recording RunOptions.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SHRINK_KEY = "memory.enable_memory_arena_shrinkage"


class _RecordingRunOptions:
    def __init__(self):
        self.entries = {}

    def add_run_config_entry(self, key, value):
        self.entries[key] = value


class _RecordingSession:
    def __init__(self):
        self.run_options = []

    def get_inputs(self):
        return [types.SimpleNamespace(name="input_ids")]

    def run(self, outputs, inputs, run_options=None):
        self.run_options.append(run_options)
        return [np.zeros(24, dtype=np.float32)]


class KokoroArenaShrinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = "_kokoro_arena_contract"
        package = ROOT / "third_party/kokoro_onnx/src/kokoro_onnx"
        native = types.ModuleType("onnxruntime")
        native.InferenceSession = object
        native.RunOptions = _RecordingRunOptions
        phonemizer = types.ModuleType("phonemizer")
        phonemizer.phonemize = Mock(side_effect=AssertionError("No native pronunciation in source contracts"))
        wrapper = types.ModuleType("phonemizer.backend.espeak.wrapper")
        wrapper.EspeakWrapper = object
        log = types.ModuleType(name + ".log")
        log.log = logging.getLogger(name)
        spec = importlib.util.spec_from_file_location(
            name, package / "__init__.py", submodule_search_locations=[str(package)]
        )
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "onnxruntime": native,
                "phonemizer": phonemizer,
                "phonemizer.backend.espeak.wrapper": wrapper,
                name: cls.module,
                name + ".log": log,
            },
        ):
            spec.loader.exec_module(cls.module)
            cls.tokenizer_class = cls.module.Tokenizer
            cls.config = sys.modules[name + ".config"]
        for imported in tuple(sys.modules):
            if imported == name or imported.startswith(name + "."):
                sys.modules.pop(imported)

    def make_model(self):
        model = self.module.Kokoro.__new__(self.module.Kokoro)
        model.tokenizer = self.tokenizer_class.__new__(self.tokenizer_class)
        model.tokenizer.vocab = self.config.DEFAULT_VOCAB
        model.tokenizer._customer = None
        model.sess = _RecordingSession()
        return model

    def synthesize_once(self, model):
        voice = np.zeros((511, 1, 256), dtype=np.float32)
        model._create_audio("həlˈoʊ", voice, 1.0)
        self.assertEqual(len(model.sess.run_options), 1)
        return model.sess.run_options[0]

    def test_every_synthesis_run_asks_the_cpu_arena_to_shrink(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIOLA_KOKORO_ARENA_SHRINK", None)
            options = self.synthesize_once(self.make_model())
        self.assertIsInstance(options, _RecordingRunOptions)
        self.assertEqual(options.entries, {SHRINK_KEY: "cpu:0"})

    def test_opt_out_restores_the_plain_run(self):
        for value in ("0", "false", "off"):
            with self.subTest(value=value), patch.dict(os.environ, {"VIOLA_KOKORO_ARENA_SHRINK": value}):
                self.assertIsNone(self.synthesize_once(self.make_model()))


if __name__ == "__main__":
    unittest.main()
