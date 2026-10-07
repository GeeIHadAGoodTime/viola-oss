"""Negative controls for customer speech dependency/payload separation."""

from __future__ import annotations

import ast
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.dependencies import verify_customer_speech
from scripts.dependencies.verify_customer_speech import (
    CJK_ROUTE_DEPENDENCIES,
    INACTIVE_CJK_REQUIRED,
    REQUIRED,
    canonical_name,
    validate_graph,
    validate_payload,
)


class CustomerSpeechDependencyTests(unittest.TestCase):
    def graph(self):
        rows = {**REQUIRED, "onnxruntime": "1.30.0"}
        return {"install": [{"metadata": {"name": name, "version": version}} for name, version in rows.items()]}

    def test_exact_speech_graph(self):
        self.assertEqual(validate_graph(self.graph()), [])

    def test_forbidden_direct_and_transitive_packages(self):
        for name in ["phonemizer", "Phonemizer_Fork", "espeakng.loader", "espeak-ng", "num2words", "misaki"]:
            report = self.graph()
            report["install"].append({"metadata": {"name": name, "version": "1.0"}})
            with self.subTest(name=name):
                self.assertTrue(validate_graph(report))

    def test_empty_incomplete_duplicate_and_changed_versions(self):
        for bad in [{}, {"install": []}, {"install": [{}]}, {"install": [{"metadata": {"name": "x"}}]}]:
            with self.subTest(bad=bad):
                self.assertTrue(validate_graph(bad))
        for name in REQUIRED:
            report = self.graph()
            report["install"] = [x for x in report["install"] if x["metadata"]["name"] != name]
            with self.subTest(missing=name):
                self.assertTrue(validate_graph(report))
            report = self.graph()
            for row in report["install"]:
                if row["metadata"]["name"] == name:
                    row["metadata"]["version"] = "unreviewed"
            with self.subTest(changed=name):
                self.assertTrue(validate_graph(report))
        report = self.graph()
        report["install"].append(copy.deepcopy(report["install"][0]))
        self.assertTrue(validate_graph(report))

    def test_native_data_python_and_internal_qa_payloads_fail(self):
        for path in [
            "_internal/espeakng_loader/espeak-ng.dll",
            "_internal/libespeak-ng.so.1",
            "phonemizer/backend/espeak/api.pyc",
            "phonemizer.backend.espeak.api",
            "num2words.lang_EN",
            "_internal/internal-use-only.json",
            "internal-speech-notices/NOTICE.txt",
            "../escape",
            "C:\\temp\\file",
        ]:
            with self.subTest(path=path):
                self.assertTrue(validate_payload([path]))
        self.assertTrue(validate_payload([]))
        self.assertEqual(
            validate_payload(["_internal/kokoro_onnx/config.json", "misaki.en", "voice.customer_pronunciation"]), []
        )


class InactiveCJKDependencyTests(unittest.TestCase):
    def graph(self):
        rows = {**REQUIRED, **INACTIVE_CJK_REQUIRED, "onnxruntime": "1.30.0"}
        return {"install": [{"metadata": {"name": name, "version": version}} for name, version in rows.items()]}

    def test_exact_cjk_pins_pass_only_when_explicitly_requested(self):
        report = self.graph()
        original = copy.deepcopy(report)
        self.assertEqual(validate_graph(report, include_cjk_prototype=True), [])
        self.assertEqual(report, original)
        english = CustomerSpeechDependencyTests().graph()
        self.assertEqual(validate_graph(english), [])
        self.assertEqual(validate_graph(english, include_cjk_prototype=False), [])
        errors = validate_graph(english, include_cjk_prototype=True)
        self.assertEqual(
            set(errors),
            {"missing or unreviewed customer speech distribution: " + name for name in INACTIVE_CJK_REQUIRED},
        )

    def test_missing_or_changed_cjk_pins_fail(self):
        for name in INACTIVE_CJK_REQUIRED:
            missing = self.graph()
            missing["install"] = [row for row in missing["install"] if row["metadata"]["name"] != name]
            changed = self.graph()
            for row in changed["install"]:
                if row["metadata"]["name"] == name:
                    row["metadata"]["version"] += ".unreviewed"
            for report in (missing, changed):
                with self.subTest(name=name, report=report):
                    self.assertEqual(
                        validate_graph(report, include_cjk_prototype=True),
                        ["missing or unreviewed customer speech distribution: " + name],
                    )

    def test_cjk_mode_retains_every_english_pin_and_runtime_requirement(self):
        for name in [*REQUIRED, "onnxruntime"]:
            report = self.graph()
            report["install"] = [row for row in report["install"] if row["metadata"]["name"] != name]
            with self.subTest(name=name):
                self.assertTrue(validate_graph(report, include_cjk_prototype=True))

    def test_old_reconstructed_companion_cannot_satisfy_corrected_identity(self):
        report = self.graph()
        for row in report["install"]:
            if row["metadata"]["name"] == "viola-misaki-cjk-prototype":
                row["metadata"]["version"] = "0.9.4+viola.cjk.2"
        self.assertEqual(
            validate_graph(report, include_cjk_prototype=True),
            ["missing or unreviewed customer speech distribution: viola-misaki-cjk-prototype"],
        )

    def test_conflicting_and_duplicate_normalized_pins_fail_in_both_orders(self):
        for name, version in INACTIVE_CJK_REQUIRED.items():
            for extra_version in (version, version + ".unreviewed"):
                for first in (True, False):
                    report = self.graph()
                    extra = {"metadata": {"name": name.upper().replace("-", "_"), "version": extra_version}}
                    report["install"].insert(0 if first else len(report["install"]), extra)
                    with self.subTest(name=name, version=extra_version, first=first):
                        self.assertIn(
                            "duplicate distribution: " + name,
                            validate_graph(report, include_cjk_prototype=True),
                        )

    def test_forbidden_extra_distribution_is_rejected(self):
        for name in ["misaki", "Phonemizer_Fork", "espeakng.loader", "num2words"]:
            report = self.graph()
            report["install"].append({"metadata": {"name": name, "version": "1.0"}})
            with self.subTest(name=name):
                self.assertIn(
                    "forbidden customer speech dependency: " + canonical_name(name),
                    validate_graph(report, include_cjk_prototype=True),
                )

    def test_unrelated_transitives_are_not_misrepresented_as_a_complete_allowlist(self):
        report = self.graph()
        report["install"].append({"metadata": {"name": "unreviewed-transitive-example", "version": "1.0"}})
        self.assertEqual(validate_graph(report, include_cjk_prototype=True), [])

    def test_english_default_does_not_silently_select_cjk(self):
        report = CustomerSpeechDependencyTests().graph()
        report["install"].append({"metadata": {"name": "viola-misaki-cjk-prototype", "version": "unreviewed"}})
        self.assertEqual(validate_graph(report), [])
        self.assertTrue(validate_graph(report, include_cjk_prototype=True))

    def test_truthy_or_falsey_non_boolean_selection_fails(self):
        for value in [None, 0, 1, "false", "true", [], {}]:
            with self.subTest(value=value):
                self.assertEqual(
                    validate_graph(self.graph(), include_cjk_prototype=value),
                    ["inactive CJK evidence selection must be an explicit boolean"],
                )

    def test_exact_pins_match_published_package_and_runtime_declarations(self):
        root = Path(__file__).resolve().parents[2]
        project = tomllib.loads((root / "third_party/misaki_cjk_prototype/pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        self.assertEqual(project["name"], "viola-misaki-cjk-prototype")
        self.assertIn(project["name"], INACTIVE_CJK_REQUIRED)
        self.assertEqual(project["version"], INACTIVE_CJK_REQUIRED[project["name"]])
        dependencies = {}
        extras = project["optional-dependencies"]
        self.assertEqual(set(extras), {"japanese", "mandarin"})
        self.assertEqual(project["dependencies"], ["viola-misaki-en==" + REQUIRED["viola-misaki-en"]])
        for requirement in project["dependencies"] + extras["japanese"] + extras["mandarin"]:
            name, separator, version = requirement.partition("==")
            self.assertEqual(separator, "==", requirement)
            name = canonical_name(name)
            self.assertNotIn(name, dependencies, requirement)
            dependencies[name] = version
        expected = dict(INACTIVE_CJK_REQUIRED)
        del expected[project["name"]]
        expected["viola-misaki-en"] = REQUIRED["viola-misaki-en"]
        self.assertEqual(dependencies, expected)

        # Parse inert declarations without importing the runtime or any frontend.
        runtime = ast.parse((root / "voice/customer_composition.py").read_text(encoding="utf-8"))
        assignments = {
            target.id: ast.literal_eval(node.value)
            for node in runtime.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id in {"_COMPANION_VERSION", "_CJK_DEPENDENCIES", "_CJK_ROUTE_DEPENDENCIES"}
        }
        self.assertEqual(assignments["_COMPANION_VERSION"], project["version"])
        del expected["viola-misaki-en"]
        self.assertEqual(assignments["_CJK_DEPENDENCIES"], expected)
        expected_routes = {
            "ja": ("fugashi", "jaconv", "mojimoji"),
            "zh": ("pypinyin", "cn2an", "jieba", "ordered-set", "proces"),
        }
        self.assertEqual(CJK_ROUTE_DEPENDENCIES, expected_routes)
        self.assertEqual(assignments["_CJK_ROUTE_DEPENDENCIES"], expected_routes)
        for locale, extra in (("ja", "japanese"), ("zh", "mandarin")):
            self.assertEqual(
                {name.partition("==")[0] for name in extras[extra]}, set(expected_routes[locale])
            )

    def selected_graph(self, locale):
        names = {"viola-misaki-cjk-prototype", *CJK_ROUTE_DEPENDENCIES[locale]}
        rows = {**REQUIRED, "onnxruntime": "1.30.0"}
        rows.update({name: INACTIVE_CJK_REQUIRED[name] for name in names})
        return {"install": [{"metadata": {"name": name, "version": version}} for name, version in rows.items()]}

    def test_selected_routes_do_not_require_the_other_language(self):
        for locale in ("ja", "zh"):
            report = self.selected_graph(locale)
            with self.subTest(locale=locale):
                self.assertEqual(validate_graph(report, include_cjk_prototype=True, cjk_locales=(locale,)), [])
                self.assertTrue(validate_graph(report, include_cjk_prototype=True))
                self.assertTrue(validate_graph(report, include_cjk_prototype=True, cjk_locales=("ja", "zh")))
        names = {row["metadata"]["name"] for row in self.selected_graph("zh")["install"]}
        self.assertFalse(names & {"fugashi", "jaconv", "mojimoji"})

    def test_each_selected_pin_missing_changed_or_duplicated_fails(self):
        for locale in ("ja", "zh"):
            for name in ("viola-misaki-cjk-prototype", *CJK_ROUTE_DEPENDENCIES[locale]):
                for damage in ("missing", "changed", "duplicate", "conflicting"):
                    report = self.selected_graph(locale)
                    row = next(r for r in report["install"] if r["metadata"]["name"] == name)
                    if damage == "missing":
                        report["install"].remove(row)
                    elif damage == "changed":
                        row["metadata"]["version"] = "unreviewed"
                    else:
                        extra = copy.deepcopy(row)
                        extra["metadata"]["name"] = name.upper().replace("-", "_")
                        if damage == "conflicting":
                            extra["metadata"]["version"] = "unreviewed"
                        report["install"].append(extra)
                    with self.subTest(locale=locale, name=name, damage=damage):
                        self.assertTrue(validate_graph(report, include_cjk_prototype=True, cjk_locales=(locale,)))

    def test_route_selection_cannot_weaken_internal_qa_rejection(self):
        for locale in ("ja", "zh"):
            for name in ("phonemizer", "espeakng-loader", "num2words"):
                report = self.selected_graph(locale)
                report["install"].append({"metadata": {"name": name, "version": "1.0"}})
                with self.subTest(locale=locale, name=name):
                    self.assertTrue(validate_graph(report, include_cjk_prototype=True, cjk_locales=(locale,)))

    def test_route_selection_is_explicit_and_nonempty(self):
        for value in ((), [], "zh", ("zh", "zh"), ("zh", "hi"), (True,), (None,)):
            with self.subTest(value=value):
                self.assertTrue(validate_graph(self.graph(), include_cjk_prototype=True, cjk_locales=value))
        self.assertTrue(validate_graph(self.graph(), cjk_locales=("zh",)))

    def test_changed_dependency_metadata_requires_new_companion_identity(self):
        for version in ("0.9.4+viola.cjk.2", "0.9.4+viola.cjk.3"):
            report = self.selected_graph("zh")
            next(r for r in report["install"] if r["metadata"]["name"] == "viola-misaki-cjk-prototype")[
                "metadata"
            ]["version"] = version
            with self.subTest(version=version):
                self.assertTrue(validate_graph(report, include_cjk_prototype=True, cjk_locales=("zh",)))

    def run_cli(self, report, *options, inventory=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            argv = ["verify_customer_speech.py", "--pip-report", str(path), *options]
            if inventory is not None:
                frozen = Path(folder) / "frozen.json"
                frozen.write_text(json.dumps(inventory), encoding="utf-8")
                argv.extend(["--frozen-inventory", str(frozen)])
            output = io.StringIO()
            before = dict(os.environ)
            with patch.object(sys, "argv", argv), redirect_stdout(output):
                code = verify_customer_speech.main()
            self.assertEqual(dict(os.environ), before)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            return code, output.getvalue()

    def test_cli_default_and_explicit_mode_are_distinct(self):
        english = CustomerSpeechDependencyTests().graph()
        code, output = self.run_cli(english)
        self.assertEqual(code, 0)
        self.assertEqual(
            output,
            "Customer speech dependency separation passed; release and listening acceptance remain separate\n",
        )
        code, output = self.run_cli(english, "--include-cjk-prototype")
        self.assertEqual(code, 1)
        self.assertIn("missing or unreviewed customer speech distribution: viola-misaki-cjk-prototype", output)
        code, output = self.run_cli(self.graph(), "--include-cjk-prototype")
        self.assertEqual(code, 0)
        self.assertEqual(
            output,
            "Inactive CJK prototype dependency separation and pins passed; "
            "graph completeness, licensing and customer release remain unqualified\n",
        )

    def test_cli_cjk_mode_preserves_frozen_contamination_checks(self):
        code, output = self.run_cli(
            self.graph(),
            "--include-cjk-prototype",
            inventory={"files": ["_internal/kokoro_onnx/config.json"], "python_modules": ["num2words.lang_EN"]},
        )
        self.assertEqual(code, 1)
        self.assertIn("forbidden customer speech payload: num2words.lang_EN", output)
        self.assertNotIn("pins passed", output)

    def test_cli_mandarin_only_selection_retains_explicit_mode(self):
        code, _output = self.run_cli(self.selected_graph("zh"), "--include-cjk-prototype", "--cjk-locales", "zh")
        self.assertEqual(code, 0)
        code, _output = self.run_cli(self.selected_graph("zh"), "--cjk-locales", "zh")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
