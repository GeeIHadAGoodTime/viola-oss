from pathlib import Path
import unittest


class TtsProfileDependencyTests(unittest.TestCase):
    def test_shared_number_normalizer_is_in_every_desktop_profile(self):
        root = Path(__file__).resolve().parents[2]
        for profile in ("requirements_desktop.txt", "requirements_linux.txt", "requirements_macos.txt"):
            with self.subTest(profile=profile):
                requirements = {line.split("#", 1)[0].strip() for line in (root / profile).read_text(encoding="utf-8").splitlines()}
                self.assertIn("num2words==0.5.14", requirements)
