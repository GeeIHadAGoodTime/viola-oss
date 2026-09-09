# Install the source core

Python 3.11 is the reference interpreter. Create and activate a virtual environment
before installing. On Windows PowerShell use `.venv\Scripts\Activate.ps1`; if local
script policy prevents activation, invoke `.venv\Scripts\python.exe` directly.
On Linux/macOS use `source .venv/bin/activate`.

Install from the source root so local dependency paths resolve correctly:

```sh
python -m pip install -r requirements_bootstrap.txt
python -m pip install -r requirements_desktop.txt
```

The selected Pipecat source lives under `third_party/pipecat/`. It is a disclosed
modified upstream component with its original license and modification record.
Do not replace it with an arbitrary upstream wheel: the selected source removes
the NLTK dependency used by older upstream phone tokenization.

## System dependencies

Windows is the reference desktop platform. Linux and macOS have separate dependency
recipes (`requirements_linux.txt`, `requirements_macos.txt`) and require native
audio/GUI libraries. WASAPI loopback is Windows-specific; it is not available on
other platforms. Install PortAudio for microphone/audio capture, and VLC only if
selecting the VLC playback backend. Linux may also need ALSA/PulseAudio development
libraries to build audio packages. Hardware access depends on OS permissions.

System TTS uses pyttsx3: Windows SAPI, macOS speech, or a system speech engine on
Linux. No speech engine binary is redistributed by this source snapshot.
This fallback speaks through the desktop's speakers. Browser/multiroom paths that
need generated audio bytes require a byte-producing provider such as optional
Kokoro; pyttsx3 does not supply that interface in Viola.

## Browser interface

Install Node.js 22 or later and build from the included lockfile:

```sh
cd ui/react-app
npm ci
npm run build
cd ../..
```

The build writes `ui/static/react/`. No company error-reporting token or private
build service is required. Leave Sentry upload variables unset for a local build.
The built UI is served by the local Python application; the default URL is
`http://127.0.0.1:8756/`.

## Configuration and models

Copy `.env.example` to `.env`, then follow [configuration](CONFIGURATION.md).
Do not overwrite an existing personal `.env`. Prepare local speech recognition:

```sh
python scripts/download_models.py --stt
```

This downloads the pinned `tiny.en` faster-whisper snapshot from Hugging Face,
verifies every file's SHA-256, and stores it under `models/faster-whisper/`.
The four wake/VAD models listed in `LICENSES/asset_inventory.toml` are included.
Larger or different user-selected models are separate downloads with their own
upstream terms. First-time local language-model setup is also separate: install
Ollama, download the model you intend to use, and choose it in Settings.

## Optional Kokoro

Install eSpeak-NG through your operating system, then install the explicit extra:

```sh
python -m pip install -r requirements_kokoro.txt
python scripts/download_models.py --kokoro
```

The source fork of Kokoro uses your system eSpeak library. If it is outside the
system search paths, set `PHONEMIZER_ESPEAK_LIBRARY` to its library file and
`PHONEMIZER_ESPEAK_DATA_PATH` to its data directory. Select Kokoro only after
installing these prerequisites. The separately installed phonemizer/eSpeak
components retain their GPL terms; they are not relicensed as Apache-2.0.

## Optional noise suppression

DeepFilterNet uses a separate Python environment because its native extension
requires NumPy 1 while the desktop uses NumPy 2. Follow the exact installation,
model preparation and environment settings in
[the DeepFilter runtime guide](../optional/deepfilter-runtime/README.md).
The desktop `requirements_optional.txt` must not install DeepFilter into the
application environment. The worker preserves audio when it is unavailable and
is used only for speech recognition, never before wake-word detection.

## Launch

```sh
python run_viola.py
```

For a desktop process without a visible Qt window, use `python run_viola.py --headless`.
Keep the server on loopback until you configure authentication, pairing and the
network exposure you actually need. Do not expose a development server directly
to the Internet. See [telephony](TELEPHONY.md) for carrier callback configuration.
