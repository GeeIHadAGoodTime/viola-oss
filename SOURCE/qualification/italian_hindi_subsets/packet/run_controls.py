"""Offline stdlib replay for the two inactive reconstruction roots."""
import argparse
import builtins
import hashlib
import importlib
import io
import json
from pathlib import Path
import socket
import sys
import time
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--language", choices=("italian", "hindi", "both"), default="both")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    selected = [("italian", "italian-stress-prototype", "test_italian_stress"),
                ("hindi", "hindi-word-prototype", "test_hindi_word")]
    selected = [entry for entry in selected if args.language in ("both", entry[0])]
    paths = sorted(path for _, directory, _ in selected for path in (args.root / directory).rglob("*") if path.is_file())
    paths += [args.root / "evidence/kokoro-config.json"]
    def hashes():
        return {str(path.relative_to(args.root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    before = hashes()
    attempts = []
    original_import = builtins.__import__
    forbidden = {"epitran", "panphon", "numpy", "torch", "onnxruntime", "regex", "misaki", "kokoro_onnx"}
    def checked_import(name, *positional, **keywords):
        if name.split(".", 1)[0] in forbidden:
            raise AssertionError("Unqualified dependency import: " + name)
        return original_import(name, *positional, **keywords)
    def no_network(*positional, **keywords):
        attempts.append("socket/DNS")
        raise AssertionError("Offline qualification attempted network access")
    builtins.__import__ = checked_import
    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        setattr(socket, name, no_network)
    try:
        socket.getaddrinfo("qualification.invalid", 443)
    except AssertionError:
        pass
    else:
        raise AssertionError("DNS guard control failed")
    assert attempts == ["socket/DNS"]
    attempts.clear()
    suite = unittest.TestSuite()
    for _, directory, module in selected:
        sys.path.insert(0, str(args.root / directory))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(module)))
    class CountResult(unittest.TextTestResult):
        subtests = 0
        def addSubTest(self, test, subtest, error):
            self.subtests += 1
            return super().addSubTest(test, subtest, error)
    stream = io.StringIO()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CountResult).run(suite)
    after = hashes()
    receipt = {
        "qualification": "fresh inactive source/representation controls",
        "tests": result.testsRun, "subtests": result.subtests, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "seconds": time.monotonic() - started, "socket_attempts": attempts,
        "source_before": before, "source_after": after, "source_unchanged": before == after,
        "stdout": stream.getvalue(), "forbidden_modules_loaded": sorted(forbidden.intersection(sys.modules)),
    }
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded)
    return 0 if result.wasSuccessful() and before == after and not attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
