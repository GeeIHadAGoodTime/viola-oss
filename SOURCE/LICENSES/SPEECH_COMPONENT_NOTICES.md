# Speech components in the source release

The default source installation uses a system speech engine or user-owned speech
provider. It contains no native eSpeak-NG library, eSpeak data, GPL phonemizer
source, or espeakng-loader 0.2.4 wheel. Optional installed components keep their
own licenses; the Apache license for Viola does not change those terms.

## Optional Kokoro

The modified MIT kokoro-onnx source is in `third_party/kokoro_onnx/`, with its
original license and modification/origin records. Install it explicitly using
`requirements_kokoro.txt`. It uses a user-installed system eSpeak-NG library and
separately installed GPL-3.0-or-later phonemizer-fork. No library/data is fetched
implicitly by a loader. eSpeak-NG source and terms are available at
https://github.com/espeak-ng/espeak-ng/tree/1.52.0 . GPL and relevant BSD texts are
preserved in this directory for reference. The rejected loader wheel is not
licensed by the later MIT notice retained here as historical provenance.

The optional checksum-pinned Kokoro model/voice downloads originate from
https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0 . Kokoro-82M
weights are Apache-2.0 under https://huggingface.co/hexgrad/Kokoro-82M . They are
prepared only when requested; they are not in the source snapshot.

## Local speech recognition and wake detection

The source downloader's `--stt` option prepares the exact Systran
faster-whisper-tiny.en conversion of OpenAI Whisper tiny.en, both published under
MIT. Each downloaded file is verified against its committed SHA-256. See
https://huggingface.co/Systran/faster-whisper-tiny.en and
https://github.com/openai/whisper/blob/main/LICENSE . Downloaded models are
separate from this source distribution.

Included Silero VAD is covered by `Silero-MIT.txt`; included wake-model hashes and
notices are mapped in `asset_inventory.toml`. User-trained and optional downloaded
models retain their own applicable terms.
