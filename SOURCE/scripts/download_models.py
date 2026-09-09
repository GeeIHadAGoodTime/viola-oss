"""Download Viola's bundled model assets with mandatory checksum verification.

Default run: the Kokoro TTS assets + the ViolaWake wake-word model.
``--stt``: the desktop faster-whisper speech-to-text snapshot (Windows release
build; the cloud image bakes its own in Dockerfile.cloud).
``--streaming-stt``: the opt-in phone streaming-STT model.

Every model this script fetches is pinned to a SHA-256 checksum. A download
that does not match its pin is deleted and counted as FAILED, and the script
exits non-zero — the Dockerfile.cloud image build (and any other caller) fails
closed instead of baking a silently-wrong model into the artifact (SEC-011).

The pins here are the canonical Kokoro pins; `.github/scripts/
provision_linux_models.py` carries the same values for the Linux CI build and
the `security-control-must-be-wired` gate (scripts/check_security_control_wired.py)
fails the build if the two files ever drift apart.
"""

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path
from typing import NamedTuple


class ModelSpec(NamedTuple):
    url: str
    sha256: str


TTS_MODELS: dict[str, ModelSpec] = {
    "kokoro-v1.0.onnx": ModelSpec(
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        sha256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
    "voices-v1.0.bin": ModelSpec(
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
    "piper/en_US-lessac-medium.onnx": ModelSpec(
        url="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx?download=true",
        sha256="5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
    "piper/en_US-lessac-medium.onnx.json": ModelSpec(
        url="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json?download=true",
        sha256="efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
}

# ---------------------------------------------------------------------------
# Desktop speech-to-text (faster-whisper) — bundled, not downloaded on first use
# ---------------------------------------------------------------------------
# The desktop default is ``tiny.en`` (config/defaults.py DEFAULT_WHISPER_MODEL),
# and until this existed nothing STT shipped in the installer at all: a brand new
# user's first push-to-talk cold-fetched 75,537,502 bytes from HuggingFace mid-turn,
# and simply failed on a machine that was offline or behind a TLS-inspecting proxy.
#
# Laid out in HuggingFace's own on-disk cache shape so that
# ``voice.transcription.whisper_model_cache.snapshot_present`` finds it and loads
# it with ``local_files_only=True`` (no network, no cache writes). This is the
# same layout ``Dockerfile.cloud`` bakes for the cloud image, so one resolver
# reads both.
#
# Fetched by URL with SHA-256 pins rather than via ``huggingface_hub`` so the
# build fails closed on a tampered or truncated model, exactly like the TTS and
# wake models above. Pins were taken from the snapshot faster-whisper itself
# resolved for revision ``STT_SNAPSHOT_REVISION``.
STT_REPO = "Systran/faster-whisper-tiny.en"
STT_MODEL_NAME = "tiny.en"
STT_SNAPSHOT_REVISION = "0d3d19a32d3338f10357c0889762bd8d64bbdeba"  # pragma: allowlist secret - public HuggingFace git revision, not a credential.
STT_BAKED_ROOT = Path("models") / "faster-whisper"

STT_MODELS: dict[str, ModelSpec] = {
    "config.json": ModelSpec(
        url=f"https://huggingface.co/{STT_REPO}/resolve/{STT_SNAPSHOT_REVISION}/config.json",
        sha256="14b1b421a90349bc551b881461426b561a874049cb9e4c4864f2ca384f6a7cc5",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
    "model.bin": ModelSpec(
        url=f"https://huggingface.co/{STT_REPO}/resolve/{STT_SNAPSHOT_REVISION}/model.bin",
        sha256="1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
    "tokenizer.json": ModelSpec(
        url=f"https://huggingface.co/{STT_REPO}/resolve/{STT_SNAPSHOT_REVISION}/tokenizer.json",
        sha256="929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
    "vocabulary.txt": ModelSpec(
        url=f"https://huggingface.co/{STT_REPO}/resolve/{STT_SNAPSHOT_REVISION}/vocabulary.txt",
        sha256="ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf",  # pragma: allowlist secret - public model file SHA-256, not a credential.
    ),
}

WAKE_MODEL_PATH = Path("violawake_data") / "trained_models" / "temporal_cnn.onnx"

# ViolaWake "Viola" wake-word model (temporal CNN over OpenWakeWord 96-dim x 9-frame
# embeddings). Published on the ViolaWake open-source repo; pinned by SHA-256 so a
# tampered/wrong model fails closed instead of shipping silently.
WAKE_MODEL_SPEC = ModelSpec(
    url="https://github.com/GeeIHadAGoodTime/ViolaWake/releases/download/v0.1.0/temporal_cnn.onnx",
    sha256="9c0b12c68593cfdb3d320a3b34667913b18d63e89eb01247d6332d7839ac9efe",  # pragma: allowlist secret - public model file SHA-256, not a credential.
)

_CHUNK = 1024 * 1024


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_directories(root: Path) -> None:
    (root / "models" / "tts").mkdir(parents=True, exist_ok=True)
    (root / "models" / "tts" / "piper").mkdir(parents=True, exist_ok=True)
    (root / "violawake_data" / "trained_models").mkdir(parents=True, exist_ok=True)


def is_non_empty_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_checksum(path: Path, expected_sha256: str) -> bool:
    """True only when ``path`` is a non-empty file matching the pinned SHA-256."""
    if not is_non_empty_file(path):
        return False
    return sha256_file(path) == expected_sha256


def format_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def download_file(url: str, destination: Path, expected_sha256: str) -> bool:
    temp_path = destination.with_suffix(destination.suffix + ".part")

    try:
        with urllib.request.urlopen(url) as response:  # nosec B310: https URL pinned in TTS_MODELS.
            total_size_header = response.headers.get("Content-Length")
            total_size = int(total_size_header) if total_size_header else 0
            bytes_downloaded = 0
            last_percent = -1

            with open(temp_path, "wb") as handle:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break

                    handle.write(chunk)
                    bytes_downloaded += len(chunk)

                    if total_size > 0:
                        percent = int((bytes_downloaded / total_size) * 100)
                        if percent != last_percent:
                            print(f"  {destination.name}: {percent}%")
                            last_percent = percent
                    else:
                        print(f"  {destination.name}: downloaded {format_size(bytes_downloaded)}")

        if not verify_model_checksum(temp_path, expected_sha256):
            actual = sha256_file(temp_path) if is_non_empty_file(temp_path) else "<empty>"
            temp_path.unlink(missing_ok=True)
            print(
                f"  CHECKSUM MISMATCH for {destination.name}: expected {expected_sha256}, got {actual}. "
                "Refusing to install an unverified model."
            )
            return False

        os.replace(temp_path, destination)
        final_size = destination.stat().st_size if destination.exists() else 0
        print(f"  Saved {destination.name} ({format_size(final_size)}) — SHA-256 verified")
        return True
    except Exception as exc:
        print(f"  Failed to download {destination.name}: {exc}")
        if temp_path.exists():
            temp_path.unlink()
        return False


def download_wake_model(root: Path) -> str:
    """Download the ViolaWake wake-word model (SHA-256 pinned). Returns status."""
    wake_model = root / WAKE_MODEL_PATH
    print()
    print("Wake word model:")
    if is_non_empty_file(wake_model):
        if verify_model_checksum(wake_model, WAKE_MODEL_SPEC.sha256):
            print(f"  Found existing wake model (SHA-256 verified) at {wake_model}")
            return "skipped"
        print("  Existing wake model failed SHA-256 verification — re-downloading.")
        wake_model.unlink(missing_ok=True)
    wake_model.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading temporal_cnn.onnx from {WAKE_MODEL_SPEC.url} ...")
    ok = download_file(WAKE_MODEL_SPEC.url, wake_model, WAKE_MODEL_SPEC.sha256)
    return "downloaded" if ok else "failed"


def stt_snapshot_dir(root: Path) -> Path:
    """Directory the baked faster-whisper snapshot files live in.

    Mirrors HuggingFace's cache layout exactly:
    ``<root>/models/faster-whisper/models--Systran--faster-whisper-tiny.en/snapshots/<rev>/``.
    ``whisper_model_cache.snapshot_present`` looks for precisely this shape.
    """
    repo_dir = "models--" + STT_REPO.replace("/", "--")
    return root / STT_BAKED_ROOT / repo_dir / "snapshots" / STT_SNAPSHOT_REVISION


def download_stt_model(root: Path) -> str:
    """Bake the desktop faster-whisper model into ``models/faster-whisper``.

    Returns ``"skipped"``, ``"downloaded"`` or ``"failed"``. Any failure is a
    build failure for the caller: shipping an installer without the model
    silently restores the first-run cold download this exists to remove.
    """
    snapshot = stt_snapshot_dir(root)
    print()
    print(f"Desktop STT model ({STT_MODEL_NAME}):")
    print(f"  Snapshot directory: {snapshot}")

    snapshot.mkdir(parents=True, exist_ok=True)
    statuses: list[str] = []
    for filename, spec in STT_MODELS.items():
        destination = snapshot / filename
        if is_non_empty_file(destination):
            if verify_model_checksum(destination, spec.sha256):
                print(f"  Skipping {filename}: already present and SHA-256 verified")
                statuses.append("skipped")
                continue
            print(f"  Existing {filename} failed SHA-256 verification — re-downloading.")
            destination.unlink(missing_ok=True)
        print(f"  Downloading {filename}...")
        if download_file(spec.url, destination, spec.sha256):
            statuses.append("downloaded")
        else:
            statuses.append("failed")

    if "failed" in statuses:
        return "failed"

    # huggingface_hub writes the resolved revision here; faster-whisper reads the
    # snapshot directly, but keeping refs/main makes the baked tree a faithful
    # cache so a later online run does not re-resolve and re-download.
    refs_main = snapshot.parent.parent / "refs" / "main"
    try:
        refs_main.parent.mkdir(parents=True, exist_ok=True)
        refs_main.write_text(STT_SNAPSHOT_REVISION, encoding="utf-8")
    except OSError as exc:
        print(f"  Failed to write {refs_main}: {exc}")
        return "failed"

    return "downloaded" if "downloaded" in statuses else "skipped"


def download_streaming_stt_model() -> None:
    """Provision the opt-in phone streaming-STT model (Phase 1).

    Kept OFF the default TTS loop on purpose: the extracted model is ~500 MB and the
    streaming feature ships default-off, so baking it into every cloud image would
    bloat the artifact. Invoke explicitly with ``--streaming-stt`` on a box/image
    that will enable ``VIOLA_PHONE_STT_STREAMING``. Imported lazily so the default
    (no-flag) build — which runs before the app code is COPYed into the image —
    never needs the ``telephony`` package.
    """
    # Running as ``python scripts/download_models.py`` puts scripts/ (not the repo
    # root) on sys.path[0], so make the repo importable before pulling in telephony.
    root = str(project_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from telephony.streaming_stt_model import (
        STREAMING_STT_MODEL_DIRNAME,
        ensure_streaming_stt_model,
        streaming_stt_model_available,
    )

    print()
    print("Streaming STT model:")
    if streaming_stt_model_available():
        print(f"  Skipping {STREAMING_STT_MODEL_DIRNAME}: already present.")
        return
    print(f"  Downloading {STREAMING_STT_MODEL_DIRNAME} (~450 MB tarball, SHA-256 verified)...")
    model_dir = ensure_streaming_stt_model()
    print(f"  Installed streaming STT model at {model_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare checksum-pinned speech assets.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--stt", action="store_true", help="Prepare local faster-whisper speech recognition")
    selection.add_argument("--streaming-stt", action="store_true", help="Prepare the optional phone streaming model")
    selection.add_argument("--kokoro", action="store_true", help="Prepare only Kokoro voices/model and the wake model")
    args = parser.parse_args()
    root = project_root()
    ensure_directories(root)

    if args.streaming_stt:
        # Fail closed: a checksum mismatch / extraction failure raises out of here
        # so the caller (operator or provisioning step) sees a non-zero exit.
        download_streaming_stt_model()
        return

    if args.stt:
        # Desktop-only: the cloud image bakes its own faster-whisper snapshot
        # (Dockerfile.cloud), so this stays OFF the default loop that
        # Dockerfile.cloud runs and is invoked explicitly by the Windows
        # release build.
        if download_stt_model(root) == "failed":
            raise SystemExit(1)
        return

    tts_dir = root / "models" / "tts"
    download_results = []

    print(f"Project root: {root}")
    print(f"TTS model directory: {tts_dir}")
    print()

    for filename, spec in TTS_MODELS.items():
        if args.kokoro and filename.startswith("piper/"):
            continue
        destination = tts_dir / filename
        if is_non_empty_file(destination):
            if verify_model_checksum(destination, spec.sha256):
                print(
                    f"Skipping {filename}: already present and SHA-256 verified ({format_size(destination.stat().st_size)})"
                )
                download_results.append((filename, "skipped"))
                continue
            print(f"Existing {filename} failed SHA-256 verification — re-downloading.")
            destination.unlink(missing_ok=True)

        print(f"Downloading {filename}...")
        destination.parent.mkdir(parents=True, exist_ok=True)
        success = download_file(spec.url, destination, spec.sha256)
        download_results.append((filename, "downloaded" if success else "failed"))

    wake_status = download_wake_model(root)
    download_results.append(("temporal_cnn.onnx (wake)", wake_status))

    downloaded = [name for name, status in download_results if status == "downloaded"]
    skipped = [name for name, status in download_results if status == "skipped"]
    failed = [name for name, status in download_results if status == "failed"]

    print()
    print("Summary:")
    if downloaded:
        print(f"  Downloaded: {', '.join(downloaded)}")
    if skipped:
        print(f"  Skipped: {', '.join(skipped)}")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    if not downloaded and not skipped and not failed:
        print("  No TTS models processed.")

    wake_model = root / WAKE_MODEL_PATH
    if is_non_empty_file(wake_model):
        print("  Wake model: present")
    else:
        print("  Wake model: missing (download failed; wake word disabled)")

    if failed:
        # Fail closed: a missing or checksum-mismatched model must fail the
        # caller (e.g. the Dockerfile.cloud image build), never ship silently.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
