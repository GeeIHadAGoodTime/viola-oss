"""Bounded customer speech contracts; native audio proof is a separate stage."""

from __future__ import annotations

import importlib.util
from decimal import Decimal, localcontext
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from voice.customer_pronunciation import CustomerTokenizer, PronunciationError, _protect_initialism_a
from voice.english_numbers import num2words

ROOT = Path(__file__).resolve().parents[2]


class EnglishNumberTests(unittest.TestCase):
    def test_cardinals_and_decimals_preserve_values(self):
        cases = {
            0: "zero",
            19: "nineteen",
            25: "twenty-five",
            100: "one hundred",
            101: "one hundred and one",
            1001: "one thousand and one",
            1100: "one thousand, one hundred",
            1000001: "one million and one",
            -42: "minus forty-two",
            "15.99": "fifteen point nine nine",
            "-0.05": "minus zero point zero five",
            "0.00001": "zero point zero zero zero zero one",
            999999999999999999999: "nine hundred and ninety-nine quintillion, nine hundred and ninety-nine quadrillion, nine hundred and ninety-nine trillion, nine hundred and ninety-nine billion, nine hundred and ninety-nine million, nine hundred and ninety-nine thousand, nine hundred and ninety-nine",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(num2words(value), expected)

    def test_fraction_digits_survive_default_and_altered_decimal_contexts(self):
        value = "12345678901234567890.12345678901234567890"
        expected_fraction = (
            "one two three four five six seven eight nine zero one two three four five six seven eight nine zero"
        )
        for precision in (2, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                for signed in (value, "-" + value):
                    self.assertEqual(num2words(signed).split(" point ")[1], expected_fraction)
                self.assertEqual(num2words(Decimal("999999999999999999999")), num2words(999999999999999999999))
                with self.assertRaises(ValueError):
                    num2words("1000000000000000000000.00000000000000000001")

    def test_ordinals_and_years(self):
        for n, expected in [
            (0, "zeroth"),
            (1, "first"),
            (2, "second"),
            (3, "third"),
            (5, "fifth"),
            (8, "eighth"),
            (9, "ninth"),
            (12, "twelfth"),
            (20, "twentieth"),
            (21, "twenty-first"),
            (100, "one hundredth"),
            (1001, "one thousand and first"),
        ]:
            with self.subTest(n=n):
                self.assertEqual(num2words(n, to="ordinal"), expected)
        for n, expected in [
            (1900, "nineteen hundred"),
            (1901, "nineteen oh-one"),
            (1999, "nineteen ninety-nine"),
            (2000, "two thousand"),
            (2007, "two thousand and seven"),
            (2026, "twenty twenty-six"),
        ]:
            with self.subTest(n=n):
                self.assertEqual(num2words(n, to="year"), expected)

    def test_bad_values_do_not_truncate_or_loop(self):
        for n in [
            True,
            False,
            None,
            "NaN",
            "Infinity",
            "-Infinity",
            "1e999999",
            "1e-999999",
            10**21,
            "9" * 1000,
            "invalid",
        ]:
            with self.subTest(n=repr(n)[:40]), self.assertRaises(ValueError):
                num2words(n)
        for kwargs in [{"to": "currency"}, {"lang": "fr"}, {"to": "ordinal"}, {"to": "year"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                num2words(1.5, **kwargs)


class CustomerAdapterTests(unittest.TestCase):
    def adapter(self, phonemes="abc", tokens=None):
        obj = CustomerTokenizer.__new__(CustomerTokenizer)
        obj.vocab = {c: i for i, c in enumerate("abc .")}
        obj._lock = threading.Lock()
        if tokens is None:
            tokens = [types.SimpleNamespace(text="word", phonemes=phonemes)]
        obj._g2p = {lang: lambda text: (phonemes, tokens) for lang in ("en-us", "en-gb")}
        return obj

    def test_formatter_and_customer_bind_the_complete_canonical_tables(self):
        import re
        import voice.customer_pronunciation as customer
        import voice.pronunciation_tables as tables

        logging = types.ModuleType("core.logging_config")
        logging.get_logger = lambda name: types.SimpleNamespace(debug=lambda *args: None)
        spec = importlib.util.spec_from_file_location(
            "_shared_pronunciation_formatter", ROOT / "voice/synthesis/text_normalizer.py"
        )
        formatter = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"core.logging_config": logging}):
            spec.loader.exec_module(formatter)
        checked = 0
        for name in ("_ACRONYM_INITIALISMS", "_ACRONYM_WORDS", "_BRAND_PRONUNCIATIONS"):
            table = getattr(tables, name)
            self.assertIs(getattr(customer, name), table)
            self.assertIs(getattr(formatter, name), table)
            for key, expansion in table.items():
                expected = re.sub(r"\bA\b", "[A](/ˈA/)", expansion)
                with self.subTest(key=key):
                    self.assertEqual(_protect_initialism_a(expansion), expected)
                checked += 1
        self.assertEqual(checked, 83)

    def test_initialism_a_is_customer_scoped_and_not_an_article(self):
        for raw, expected in [
            ("A I", "[A](/ˈA/) I"),
            ("A P I", "[A](/ˈA/) P I"),
            ("A M D", "[A](/ˈA/) M D"),
            ("A A M D chip", "A [A](/ˈA/) M D chip"),
            ("Open A I", "Open [A](/ˈA/) I"),
            ("A book and A I", "A book and [A](/ˈA/) I"),
            ("A B A", "A B A"),
            ("A I.", "[A](/ˈA/) I."),
            ("A P I, please.", "[A](/ˈA/) P I, please."),
            ("“A I” and (A P I).", "“[A](/ˈA/) I” and ([A](/ˈA/) P I)."),
            ("'A P I'", "'[A](/ˈA/) P I'"),
            ("Read 'A P I.' please.", "Read '[A](/ˈA/) P I.' please."),
            ("O'A P I'", "O'A P I'"),
            ("A P I-like", "A P I-like"),
            ("A U S citizen", "A U S citizen"),
            ("A C P U", "A C P U"),
            ("A C I A agent", "A C I [A](/ˈA/) agent"),
            ("A A P I response", "A [A](/ˈA/) P I response"),
            ("A F A Q page", "A F [A](/ˈA/) Q page"),
            ("L A", "L [A](/ˈA/)"),
            ("A U.S. citizen", "A U.S. citizen"),
            ("A C++ developer", "A C++ developer"),
            ("A I/O error", "A I/O error"),
            ("A B2 example", "A B2 example"),
            ("X/A I", "X/A I"),
            ("A book", "A book"),
            ("a I", "a I"),
            ("A Item", "A Item"),
            ("[A I](/həlˈO/) and A P I", "[A I](/həlˈO/) and [A](/ˈA/) P I"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(_protect_initialism_a(raw), expected)

    def test_initialism_transform_reaches_only_the_customer_g2p_boundary(self):
        obj = self.adapter()
        inputs = []
        obj._g2p["en-us"] = lambda text: (
            inputs.append(text) or "abc",
            [types.SimpleNamespace(text="word", phonemes="abc")],
        )
        obj.phonemize("A I")
        obj.phonemize("A book")
        self.assertEqual(inputs, ["[A](/ˈA/) I", "A book"])

    def test_supported_dialects_and_lossless_tokens(self):
        obj = self.adapter()
        self.assertEqual(obj.phonemize("hello", lang="en_US"), "abc")
        self.assertEqual(obj.phonemize("hello", lang="en-gb"), "abc")
        self.assertEqual(obj.tokenize("abc"), [0, 1, 2])

    def test_customer_route_never_silently_drops_unknowns(self):
        for phones, tokens in [
            ("abc❓", None),
            ("abc", [types.SimpleNamespace(text="word", phonemes=None)]),
            ("", []),
            ("abc🙂", None),
        ]:
            obj = self.adapter(phones, tokens)
            with self.subTest(phones=phones), self.assertRaises(PronunciationError):
                obj.phonemize("private input")
        obj = self.adapter()
        for phones in ["", "a" * 511, "abc❓"]:
            with self.subTest(phones=phones[:20]), self.assertRaises(PronunciationError):
                obj.tokenize(phones)

    def test_empty_explicit_overrides_cannot_drop_words(self):
        obj = self.adapter()
        for phones in (" ", "", ",", ".", "\u200b", "ˈˌ"):
            with self.subTest(phones=phones), self.assertRaises(PronunciationError):
                obj.phonemize("Hello [outofdictionaryxyz](/" + phones + "/) world.")
        # Explicit spoken overrides remain supported.
        self.assertEqual(obj.phonemize("Hello [name](/abc/) world."), "abc")
        for phones in ("", " ", ","):
            obj = self.adapter("abc", [types.SimpleNamespace(text="word", phonemes=phones)])
            with self.subTest(token_phones=phones), self.assertRaises(PronunciationError):
                obj.phonemize("word")

    def test_no_language_guess_or_silent_input_truncation(self):
        obj = self.adapter()
        for lang, text in [("fr-fr", "bonjour"), ("en", "hello"), ("en-us", ""), ("en-us", "a" * 5001)]:
            with self.subTest(lang=lang), self.assertRaises(PronunciationError):
                obj.phonemize(text, lang)

    def test_offline_missing_fork_is_an_error_before_nlp_import(self):
        with (
            patch("importlib.metadata.version", return_value="wrong"),
            self.assertRaisesRegex(RuntimeError, "reviewed"),
        ):
            CustomerTokenizer({"a": 1})

    def test_customer_backend_imports_no_espeak_path(self):
        name = "_customer_tokenizer_contract"
        folder = ROOT / "third_party/kokoro_onnx/src/kokoro_onnx"
        package = types.ModuleType(name)
        package.__path__ = [str(folder)]
        package._require_customer_telemetry_opt_out = lambda: None
        log = types.ModuleType(name + ".log")
        log.log = types.SimpleNamespace()
        module_spec = importlib.util.spec_from_file_location(name + ".tokenizer", folder / "tokenizer.py")
        module = importlib.util.module_from_spec(module_spec)
        with (
            patch.dict(
                sys.modules,
                {name: package, name + ".log": log, "phonemizer": None, "phonemizer.backend.espeak.wrapper": None},
            ),
            patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"}),
            patch("voice.customer_pronunciation.CustomerTokenizer", return_value=self.adapter()) as customer,
        ):
            module_spec.loader.exec_module(module)
            tokenizer = module.Tokenizer()
            self.assertEqual(tokenizer.phonemize("hello"), "abc")
            self.assertEqual(tokenizer.tokenize("abc"), [0, 1, 2])
            customer.assert_called_once()
            with self.assertRaisesRegex(ValueError, "eSpeak configuration"):
                module.Tokenizer(espeak_config=object())
        for key in tuple(sys.modules):
            if key.startswith(name):
                sys.modules.pop(key, None)


class RetainedFormatterCurrencyTests(unittest.TestCase):
    def test_real_currency_formatter_preserves_fraction_values(self):
        import ast
        import re

        source = ROOT / "voice/synthesis/text_normalizer.py"
        names = {"_CURRENCY_RE", "_DIGIT_NAMES", "_clean_num2words_output", "_number_to_words", "_replace_currency"}
        nodes = []
        for node in ast.parse(source.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
                nodes.append(node)
        namespace = {"re": re, "num2words": num2words}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        cases = {
            "$1.5": "one dollar and fifty cents",
            "$0.5": "fifty cents",
            "$1.05": "one dollar and five cents",
            "$12.345": "twelve point three four five dollars",
            "$12.000": "twelve dollars",
            "$0.001": "zero point zero zero one dollars",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(namespace["_CURRENCY_RE"].sub(namespace["_replace_currency"], text), expected)


class CustomerTelemetryImportGuardTests(unittest.TestCase):
    def test_selected_non_windows_profile_requires_preimport_opt_out(self):
        import ast

        source = ROOT / "third_party/kokoro_onnx/src/kokoro_onnx/__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_require_customer_telemetry_opt_out"
        )
        guard_call = next(
            i
            for i, n in enumerate(tree.body)
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == function.name
        )
        ort_import = next(
            i
            for i, n in enumerate(tree.body)
            if isinstance(n, ast.Import) and any(a.name == "onnxruntime" for a in n.names)
        )
        self.assertLess(guard_call, ort_import)
        namespace = {
            "os": os,
            "sys": types.SimpleNamespace(platform="linux"),
            "_CUSTOMER_PROFILE_AT_IMPORT": True,
            "_ORT_OPT_OUT_AT_IMPORT": True,
            "_ORT_PRESENT_AT_IMPORT": False,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        for value in (None, "", "0", "true"):
            with self.subTest(value=value), patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"}):
                if value is None:
                    os.environ.pop("ORT_DISABLE_TELEMETRY", None)
                else:
                    os.environ["ORT_DISABLE_TELEMETRY"] = value
                with self.assertRaisesRegex(RuntimeError, "before Python starts"):
                    namespace[function.name]()
        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en", "ORT_DISABLE_TELEMETRY": "1"}):
            namespace[function.name]()
        for name, unsafe in (
            ("_CUSTOMER_PROFILE_AT_IMPORT", False),
            ("_ORT_OPT_OUT_AT_IMPORT", False),
            ("_ORT_PRESENT_AT_IMPORT", True),
        ):
            previous = namespace[name]
            namespace[name] = unsafe
            with (
                self.subTest(import_provenance=name),
                patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en", "ORT_DISABLE_TELEMETRY": "1"}),
                self.assertRaises(RuntimeError),
            ):
                namespace[function.name]()
            namespace[name] = previous
        kokoro = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Kokoro")
        for method_name in ("__init__", "from_session"):
            method = next(n for n in kokoro.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
            self.assertIsInstance(method.body[0], ast.Expr)
            self.assertEqual(method.body[0].value.func.id, function.name)
        # No accidental activation/change to the separately retained reference profile.
        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "espeak", "ORT_DISABLE_TELEMETRY": "0"}):
            namespace[function.name]()


if __name__ == "__main__":
    unittest.main()
