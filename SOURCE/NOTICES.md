# Source distribution notices

Original Viola code and commissioned artwork are licensed under Apache-2.0
(`LICENSE`). Third-party files, libraries, models and fonts retain their own terms.
The source license does not grant trademark rights or access to the separately
operated hosted service. No installer, Python interpreter, Qt runtime, eSpeak
library/data or private operations history is contained in this source snapshot.

## Included third-party source

The modified Pipecat and optional Kokoro source directories under `third_party/`
carry their upstream license, exact origin and modification records. These
components are not represented as unmodified upstream releases. Read those records
before replacing them with upstream packages; the modifications are part of the
supported dependency and system-speech boundary.

External Python/npm dependencies are installed separately by their package
managers. The release's resolved SBOM records exact selected versions and artifact
hashes for each supported dependency scope. Preserve the licenses supplied by those
distributions when redistributing installed dependencies or built browser assets.
The application license does not relicense any dependency.

## Models, font and artwork

`LICENSES/asset_inventory.toml` maps included assets to exact content hashes,
origins and the accompanying license text. In particular:

| Material | Origin | License and included notice |
|---|---|---|
| `models/vad/silero_vad.onnx` | Silero VAD v6.2.1 | MIT, `LICENSES/Silero-MIT.txt`, copyright Silero Team |
| OpenWakeWord embedding and melspectrogram models | OpenWakeWord v0.5.1, Google-derived backbone | Apache-2.0, `ui/react-app/public/licenses/ViolaWake-LICENSE.txt` and `ui/react-app/public/wake/NOTICE-violawake.txt` |
| ViolaWake temporal CNN | ViolaWake v0.1.0 | Apache-2.0, `violawake_data/trained_models/ViolaWake-LICENSE.txt` and `ui/react-app/public/wake/NOTICE-violawake.txt` |
| Cormorant Garamond 4.001 | Cormorant Project Authors | OFL-1.1, `ui/react-app/public/licenses/CormorantGaramond-OFL.txt` |
| Meteocons weather icons | Bas Milius | MIT, `LICENSES/meteocons.txt` |
| Viola icons/logos | Original commissioned Viola artwork | Apache-2.0, `LICENSE`; trademarks remain separate |

Separately downloaded Whisper/Kokoro models and user-selected models are not
covered merely by their runtime library's license. Read the upstream model terms
and the [speech notice](LICENSES/SPEECH_COMPONENT_NOTICES.md). The source downloader
pins and verifies the artifacts it fetches; those optional downloads are separate
from the files in the source manifest.

## Qt and optional speech

The desktop requirements install PySide6/Qt under their own LGPL/GPL or commercial
terms. No Qt DLL or Chromium runtime is redistributed in this source-only snapshot.
`LICENSES/QT-PYSIDE6-NOTICE.md` identifies upstream source/license locations; a future
binary distributor must separately satisfy the applicable replacement, source,
attribution and other distribution requirements.

Kokoro is optional. Its separately installed phonemizer and system eSpeak-NG retain
GPL-3.0-or-later terms. The default recipe does not install them. The rejected
`espeakng-loader==0.2.4` distribution is not used. A historical later-upstream MIT
notice, where retained as reference, does not retroactively license that wheel.
The first-party `espeakng_loader` system locator is original Apache-2.0 source and
contains no native library or speech data.
