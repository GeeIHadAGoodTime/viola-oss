# Viola source core

Viola is a voice assistant with a desktop and browser interface, local music,
multiroom audio, user-owned AI providers, local models, and self-hosted telephony.
This source distribution needs no Viola subscription. Company hosting, accounts,
billing, payment processing, operational services and private relays are separate.

## Start from source

Use Python 3.11 and Node.js 22 or later. Run these commands from the source folder:

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements_bootstrap.txt
python -m pip install -r requirements_desktop.txt
python -c "import shutil; shutil.copyfile('.env.example', '.env')"
cd ui/react-app
npm ci
npm run build
cd ../..
python scripts/download_models.py --stt
python run_viola.py
```

See [installation](docs/INSTALLATION.md) for platform dependencies and activation
details. The environment example selects personal desktop mode, local phone mode,
BYOK (bring your own provider key), and system speech output. Enter your AI provider
key in Settings, or select a [local model](docs/CONFIGURATION.md). Do not sign into
a company account to use the source core. Only provider features you configure
need provider credentials.

The model download is explicit and verifies checksums. Python/npm installation,
speech-model preparation, browser installation and optional local-model download
need Internet access. Once prepared, local inference and local speech run on your
machine; cloud AI, carrier calls, streaming music and Internet tools naturally
contact their chosen providers.

## Optional features

- [Self-hosted telephony](docs/TELEPHONY.md): your carrier account, local call
  history, and your own AI/speech providers.
- [Kokoro speech](docs/INSTALLATION.md#optional-kokoro): optional neural speech
  using separately installed system eSpeak-NG. No eSpeak binaries/data or rejected
  loader wheel are supplied in the default install.
- Browser tools: run `python -m playwright install chromium` before using them.
- Multiroom playback: start `python viola_spoke.py --help` on a second device
  and pair it with your local hub.

## Development and terms

[Contribution guide](CONTRIBUTING.md), [security reporting](SECURITY.md),
[license and artifact notices](NOTICES.md), and
[verification instructions](docs/VERIFYING.md) accompany this source.

Original Viola code and commissioned artwork are under [Apache-2.0](LICENSE).
Third-party code, models, icons and fonts retain their own licenses. Trademarks
and the separate hosted service are not licensed by the source-code grant.
