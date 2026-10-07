"""Exact inherited-source and targeted mutant controls; no native imports."""

import ast
import hashlib
import importlib.abc
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORBIDDEN = {"onnxruntime", "numpy", "phonemizer", "espeakng_loader", "piper_plus_g2p"}


class DenyNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FORBIDDEN:
            raise AssertionError("Forbidden import: " + fullname)


sys.meta_path.insert(0, DenyNative())
sys.path.insert(0, str(ROOT))
controls = importlib.import_module("test_romance_coverage")
CoveredRomance = importlib.import_module("romance_coverage").CoveredRomance

records = json.loads((ROOT / "upstream-bindings.json").read_text())
modifications = json.loads((ROOT / "upstream-modifications.json").read_text())
checked = []
for row in records["files"]:
    original = Path(row["file"]).read_bytes()
    assert hashlib.sha256(original).hexdigest() == row["sha256"]
    # SHA256 above checks integrity; SHA1 only reproduces the upstream Git object ID.
    assert (
        hashlib.sha1(b"blob " + str(len(original)).encode() + b"\0" + original, usedforsecurity=False).hexdigest()
        == row["git_sha"]
    )
    name = Path(row["path"]).name
    current = (ROOT / "_romance_vendor" / name).read_bytes()
    if name in {"base.py", "LICENSE"}:
        assert current == original
        checked.append({"path": name, "exact_original": True})
        continue
    change = next(x for x in modifications if x["path"] == "_romance_vendor/" + name)
    assert hashlib.sha256(current).hexdigest() == change["modified_sha256"]
    tree = ast.parse(current)
    replacement = ast.parse("i += 1").body[0]
    count = [0]

    class RestoreUnknownSkip(ast.NodeTransformer):
        def visit_Raise(self, node):
            if (
                isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "ValueError"
            ):
                if "Unsupported grapheme" in ast.unparse(node.exc):
                    count[0] += 1
                    return replacement
            return node

    restored = RestoreUnknownSkip().visit(tree)
    assert count == [1]
    assert ast.dump(restored, include_attributes=False) == ast.dump(ast.parse(original), include_attributes=False)
    checked.append({"path": name, "one_unknown_skip_replaced": True, "residual_ast_exact": True})

source = (ROOT / "romance_coverage.py").read_text()
mutants = [
    (
        "trill_character_join",
        '"rr": "r"',
        '"rr": "rr"',
        "test_trill_is_distinct_from_tap_and_double_character_join",
        (),
    ),
    (
        "rounded_vowel_as_back_vowel",
        '"y_vowel": "y"',
        '"y_vowel": "u"',
        "test_french_rounded_vowel_is_not_u_and_not_piper_pua",
        (),
    ),
    (
        "drop_prosodic_stress",
        "inserted = expected_marker",
        'inserted = ""',
        "test_french_rounded_vowel_is_not_u_and_not_piper_pua",
        (),
    ),
    (
        "discard_numeric_currency_source",
        "frontend_text, spans, expected = self._source(text)",
        'text = text.replace("123.45", "").replace("€", "").replace("世界", "")\n'
        "        frontend_text, spans, expected = self._source(text)",
        "test_numbers_currency_mixed_scripts_and_control_gaps_never_disappear",
        ("es", "hello 123.45 € 世界"),
    ),
]
mutant_results = []
for name, old, new, test, args in mutants:
    assert source.count(old) == 1
    module = types.ModuleType("_romance_mutant_" + name)
    module.__file__ = str(ROOT / "romance_coverage.py")
    exec(compile(source.replace(old, new), module.__file__, "exec"), module.__dict__)
    previous = controls.CoveredRomance, controls.CoverageError, controls.lower_units
    controls.CoveredRomance, controls.CoverageError, controls.lower_units = (
        module.CoveredRomance,
        module.CoverageError,
        module.lower_units,
    )
    try:
        getattr(controls, test)(*args)
    except AssertionError as exc:
        mutant_results.append({"mutant": name, "rejected_by": test, "reason": str(exc)})
    else:
        raise AssertionError("Mutant survived: " + name)
    finally:
        controls.CoveredRomance, controls.CoverageError, controls.lower_units = previous

# Preserve exact bounded quality witnesses rather than infer phonological gold.
limits = [
    CoveredRomance(locale, controls.VOCAB).phonemize(text)
    for locale, text in [("fr", "les amis"), ("pt-br", "noite"), ("pt-pt", "noite")]
]
assert not FORBIDDEN.intersection(sys.modules)
result = {
    "upstream_commit": records["commit"],
    "provenance": checked,
    "meaningful_mutants_rejected": mutant_results,
    "retained_quality_limits": limits,
    "native_modules_loaded": [],
    "customer_or_release_eligible": False,
}
(ROOT / "provenance-and-mutants.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(
    json.dumps(
        {
            "provenance_controls": len(checked),
            "mutants_rejected": len(mutant_results),
            "retained_quality_limits": len(limits),
        }
    )
)
