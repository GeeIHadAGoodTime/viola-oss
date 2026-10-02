from pathlib import Path


def test_shared_number_normalizer_dependency_is_in_every_desktop_profile():
    root = Path(__file__).resolve().parents[2]
    for profile in ("requirements_desktop.txt", "requirements_linux.txt", "requirements_macos.txt"):
        requirements = {line.split("#", 1)[0].strip() for line in (root / profile).read_text().splitlines()}
        assert "num2words==0.5.14" in requirements, profile
