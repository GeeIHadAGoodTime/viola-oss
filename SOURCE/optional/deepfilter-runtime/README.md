# DeepFilterNet compatibility package

This optional package builds `deepfilternet==0.5.6+viola.1` from the official
DeepFilterNet 0.5.6 wheel, verified against the fixed SHA-256 in `build_backend.py`.
The upstream code remains under the author's MIT or Apache-2.0 license; we use
the MIT option and preserve the full grant and original wheel license. Original
Viola modifications remain under Apache-2.0.

Upstream: https://github.com/Rikorose/DeepFilterNet/tree/v0.5.6
Author: Hendrik Schroeter. Published wheel: https://pypi.org/project/DeepFilterNet/0.5.6/

Changes: replace `df/io.py` with the adjacent functional SoundFile/SciPy adapter;
require modern torch without torchaudio; explicitly use weights-only checkpoint
loading. All other upstream files are unchanged. The builder records the local
version and regenerates every wheel RECORD checksum. Security scans must also
query upstream version 0.5.6, so the local version is not an advisory exception.

Installation downloads the official wheel from the pinned HTTPS URL. To build
offline, set `VIOLA_DEEPFILTER_UPSTREAM_WHEEL` to an already downloaded copy with
the expected hash; the same verification is applied. No network is used by the
running worker: configure an existing local model directory containing config.ini
and checkpoints through `VIOLA_DEEPFILTER_MODEL`.

From the Viola source root, create a separate Python 3.11 environment and install
`requirements_deepfilter.txt` into it. Set `VIOLA_DEEPFILTER_PYTHON` to that
environment's Python executable. The application process keeps its NumPy 2
dependencies; the worker uses the separately installed NumPy 1 stack.

For example, in PowerShell from the source root:

```powershell
py -3.11 -m venv .venv-deepfilter
.venv-deepfilter/Scripts/python.exe -m pip install -r requirements_deepfilter.txt
# Explicit provisioning downloads the upstream DeepFilterNet3 model into its cache.
.venv-deepfilter/Scripts/python.exe -c "from df.enhance import maybe_download_model; print(maybe_download_model('DeepFilterNet3'))"
$env:VIOLA_DEEPFILTER_PYTHON = (Resolve-Path .venv-deepfilter/Scripts/python.exe).Path
$env:VIOLA_DEEPFILTER_MODEL = '<the model directory printed above>'
```

Set the two variables in the environment that launches Viola. On POSIX systems,
use the environment's `bin/python` executable and shell `export` syntax. Choose
the `deepfilter` backend in the audio settings to enable it. No model weights are
bundled with Viola. The optional worker is used only for speech-to-text audio;
wake-word detection continues to receive its existing audio input.

The supported interface is CPU inference through `audio_core/deepfilter_worker.py`.
The retained upstream training tools are outside this compatibility package's
tested surface. Tests cover the real model at 16, 44.1 and 48 kHz, floating-point
and signed 16-bit input, exact output length, repeated requests in one worker,
shutdown, and missing model/interpreter fallback. A stationary-noise fixture
checks real signal alteration and lower noise energy; it does not establish
speech intelligibility or microphone-specific recognition quality.
