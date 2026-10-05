"""Negative controls for customer speech dependency/payload separation."""

from __future__ import annotations

import copy
import unittest

from scripts.dependencies.verify_customer_speech import REQUIRED, validate_graph, validate_payload


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


if __name__ == "__main__":
    unittest.main()
