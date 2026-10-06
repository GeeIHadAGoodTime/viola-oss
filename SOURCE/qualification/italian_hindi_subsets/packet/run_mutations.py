"""Bounded source sensitivity checks in disposable owned copies."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
MUTANTS = [
    ("italian_raw_case", "italian-stress-prototype/italian_stress.py",
     'case_view = unicodedata.normalize("NFC", source)', 'case_view = source'),
    ("italian_hard_sc", "italian-stress-prototype/italian_stress.py",
     '"<SC_HARD>": "sk"', '"<SC_HARD>": "ʃ"'),
    ("italian_soft_sc", "italian-stress-prototype/italian_stress.py",
     '"<SC_SOFT>": "ʃ"', '"<SC_SOFT>": "ʧ"'),
    ("italian_lost_stress", "italian-stress-prototype/italian_stress.py",
     'output.insert(stress_index, _Unit("ˈ", frozenset(stress_owners)))', 'pass  # deliberately lose explicit stress'),
    ("italian_lost_owners", "italian-stress-prototype/italian_stress.py",
     'Phone(unit.char, tuple(sorted(unit.owners)))', 'Phone(unit.char, (0,))'),
    ("italian_sci_guess", "italian-stress-prototype/italian_stress.py",
     'if re.search(r"sci(?=[aeou])", normalized):', 'if False:'),
    ("italian_mixed_case", "italian-stress-prototype/italian_stress.py",
     'if not (case_view.islower() or case_view.isupper() or case_view.istitle()):', 'if False:'),
    ("italian_rule_drift", "italian-stress-prototype/italian_stress.py",
     'if hashlib.sha256(raw).hexdigest() != expected:', 'if name != "strip" and hashlib.sha256(raw).hexdigest() != expected:'),
    ("hindi_initial_vowel_loss", "hindi-word-prototype/hindi_word.py",
     'if "अं" in word:', 'if False:'),
    ("hindi_final_anusvara", "hindi-word-prototype/hindi_word.py",
     'if word.endswith("ं"):', 'if False:'),
    ("hindi_unsupported_features", "hindi-word-prototype/hindi_word.py",
     'if any(char in _UNQUALIFIED_PHONES for char in mapped):', 'if False:'),
    ("hindi_missing_vocab", "hindi-word-prototype/hindi_word.py",
     'if not output or any(unit.char not in self.vocabulary for unit in output):', 'if not output:'),
    ("hindi_lost_owners", "hindi-word-prototype/hindi_word.py",
     'Phone(unit.char, tuple(sorted(unit.owners)))', 'Phone(unit.char, (0,))'),
    ("hindi_nasal_length_order", "hindi-word-prototype/hindi_word.py",
     'inserted += output[first:last]', 'inserted = output[first:last] + inserted'),
    ("hindi_lost_rewrite_audit", "hindi-word-prototype/hindi_word.py",
     'tuple(events), consumed)', '(), consumed)'),
    ("hindi_wrong_affricate", "hindi-word-prototype/hindi_word.py",
     '("d͡ʒ", "ʤ")', '("d͡ʒ", "ʧ")'),
]


def main():
    destination = ROOT / "evidence/mutations"
    destination.mkdir(exist_ok=True)
    source_paths = sorted({relative for _, relative, _, _ in MUTANTS})
    def hashes():
        return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in source_paths}
    before = hashes()
    records = []
    for name, relative, needle, replacement in MUTANTS:
        with tempfile.TemporaryDirectory(prefix="speech-subset-mutation-") as temporary:
            copy = Path(temporary)
            for directory in ("italian-stress-prototype", "hindi-word-prototype"):
                shutil.copytree(ROOT / directory, copy / directory)
            (copy / "evidence").mkdir()
            shutil.copyfile(ROOT / "evidence/kokoro-config.json", copy / "evidence/kokoro-config.json")
            target = copy / relative
            text = target.read_text()
            if text.count(needle) != 1:
                raise AssertionError(f"Mutant must change exactly one site: {name}")
            target.write_text(text.replace(needle, replacement))
            receipt = destination / (name + ".json")
            command = [sys.executable, "-B", str(ROOT / "run_controls.py"), "--root", str(copy), "--output", str(receipt)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            if not receipt.exists():
                raise AssertionError(f"Mutant failed during setup: {name}: {result.stderr}")
            observed = json.loads(receipt.read_text())
            valid = result.returncode == 1 and observed["failures"] > 0 and observed["errors"] == 0
            record = {"name": name, "candidate_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                      "exit": result.returncode, "failures": observed["failures"], "errors": observed["errors"],
                      "tests": observed["tests"], "source_unchanged": observed["source_unchanged"],
                      "socket_attempts": observed["socket_attempts"], "assertion_sensitive": valid}
            records.append(record)
            if not valid:
                raise AssertionError(json.dumps(record))
    after = hashes()
    if before != after:
        raise AssertionError("Author source changed")
    (ROOT / "evidence/mutation-summary.json").write_text(json.dumps({
        "mutants": records, "author_source_before": before, "author_source_after": after,
        "author_source_unchanged": before == after,
    }, indent=2) + "\n")
    print(f"{len(records)} mutants rejected by clean assertions; author bytes unchanged")


if __name__ == "__main__":
    main()
