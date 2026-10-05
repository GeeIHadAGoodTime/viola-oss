import ast
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


class TtsProfileDependencyTests(unittest.TestCase):
    def test_shared_number_normalizer_is_in_every_desktop_profile(self):
        root = Path(__file__).resolve().parents[2]
        for profile in ("requirements_desktop.txt", "requirements_linux.txt", "requirements_macos.txt"):
            with self.subTest(profile=profile):
                requirements = {line.split("#", 1)[0].strip() for line in (root / profile).read_text(encoding="utf-8").splitlines()}
                self.assertFalse(any(line.startswith("num2words") for line in requirements))
                self.assertTrue((root / "voice/english_numbers.py").is_file())


class KokoroNativeReadinessTests(unittest.TestCase):
    def load_probe(self):
        root = Path(__file__).resolve().parents[2]
        source = root / "voice/synthesis/kokoro_engine.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_probe_kokoro_package")
        namespace = {"_KOKORO_PKG_PROBE_ERROR": None, "logger": Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        return namespace

    def test_missing_native_phonemizer_is_not_reported_available(self):
        package = ModuleType("kokoro_onnx")
        tokenizer = ModuleType("kokoro_onnx.tokenizer")
        tokenizer.Tokenizer = Mock(side_effect=RuntimeError("system eSpeak-NG is missing"))
        with patch.dict("sys.modules", {"kokoro_onnx": package, "kokoro_onnx.tokenizer": tokenizer}):
            namespace = self.load_probe()
            error = namespace["_probe_kokoro_package"]()
        self.assertIn("eSpeak-NG", error)
        tokenizer.Tokenizer.assert_called_once_with()

    def test_native_phonemizer_readiness_is_checked_once_without_loading_model(self):
        package = ModuleType("kokoro_onnx")
        package.Kokoro = Mock(side_effect=AssertionError("readiness must not allocate model"))
        tokenizer = ModuleType("kokoro_onnx.tokenizer")
        tokenizer.Tokenizer = Mock()
        with patch.dict("sys.modules", {"kokoro_onnx": package, "kokoro_onnx.tokenizer": tokenizer}):
            namespace = self.load_probe()
            self.assertEqual(namespace["_probe_kokoro_package"](), "")
            self.assertEqual(namespace["_probe_kokoro_package"](), "")
        tokenizer.Tokenizer.assert_called_once_with()
        package.Kokoro.assert_not_called()
