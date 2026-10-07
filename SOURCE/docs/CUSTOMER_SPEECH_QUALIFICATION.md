# Customer speech component qualification

Status: English and explicit Japanese/Mandarin source-QA candidates; not customer-release-qualified.

## Recovered decision and scope

The historical `docs/TTS_ENGINE_RESEARCH.md` in the private service repository
records Kokoro ONNX with Misaki English and no eSpeak fallback. Its claim that
all dependencies were permissive and the path was already complete was not
supported by the actual tokenizer, which used phonemizer and eSpeak. The same
historical document also describes 48 voices/eight offline languages. The
current phone roadmap preserves eight locales. The current product requirement
is all eight language families and all 54 canonical voices; the historical
48-voice sentence is not the current acceptance target. There is no recovered
approval to reduce that product scope to English.

The default customer component is English. The explicitly constructed, inactive
CJK companion now has the bounded source evidence below. Do not activate either
for a customer build until the full multilingual/voice-selection scope is qualified. Desktop's existing hardcoded `en-us` call is implementation evidence,
not permission to remove other promised capabilities. The environment selector
would also affect local phone Kokoro, so its eight-language source forwarding
tests alone cannot qualify a customer artifact using this profile.

## Implementation

- Kokoro neural inference, voices, chunking, speech shaping, ducking, output
  routing, volume and post-processing remain in their existing paths.
- `requirements_customer_speech.txt` installs maintained Kokoro and English-only
  Misaki forks plus the checksum-pinned English spaCy model. It never installs
  the upstream Misaki English extra, eSpeak loader, phonemizer or num2words.
- Explicit `VIOLA_KOKORO_PHONEMIZER=misaki-en` selects the English adapter.
  Default/source and internal-reference paths remain unchanged unless selected.
  On non-Windows the selected wrapper fails before its ONNX import unless
  `ORT_DISABLE_TELEMETRY=1` is already set; application-wide startup ordering
  remains a separate gate. Late profile selection or preloaded ONNX Runtime is
  rejected conservatively, including `Tokenizer`, `Kokoro` and `from_session`
  construction. Current phone source runs the reviewed Kokoro startup guard
  before application/Pipecat imports and again before the loader's ONNX import.
  This guarded source ordering does not establish every application ingress,
  frozen startup or Windows ETW/privacy behavior; those remain separate gates.
- Misaki 0.9.4's English algorithm and four lexicons are retained with reviewed exact-decimal and
  currency-value preservation fixes. The caller
  supplies a local spaCy tagger and independently written English number
  converter. The fork has no runtime model downloader or pronunciation fallback.
- The English adapter accepts `en-us`/`en-gb`, adds exact fragments already used by Viola's
  pronunciation tables, and rejects unknown words, unsupported symbols and
  other locales before neural inference. Errors do not include utterance text.
- Existing public number formatting now uses `voice/english_numbers.py` rather
  than importing LGPL num2words. Its bounded cardinal/ordinal/year contract is
  checked against the former implementation using a test-only reference.

Unknown words still require a pronunciation override. This is an explicit
coverage limitation, not a reason to silently delete words or substitute a
lower-quality backend in the qualification evidence.

## License and source evidence

- Misaki 0.9.4 code/retained lexicons: Apache-2.0. Exact source archive, original
  file hashes, modification patch and resulting file hashes are recorded in
  `third_party/sources.toml`; original license is retained in
  `third_party/misaki_en/LICENSE`.
- Kokoro ONNX wrapper: MIT. Its original license and reproducible source patch
  remain in `third_party/kokoro_onnx/` and `third_party/sources.toml`.
- Kokoro model/voices use the existing canonical artifact hashes in
  `scripts/download_models.py`; model origin and Apache-2.0 notice remain in
  `LICENSES/SPEECH_COMPONENT_NOTICES.md`.
- spaCy and `en_core_web_sm` 3.8.0: MIT. The model wheel is fetched from the
  official spaCy release with its committed SHA-256, never during first speech.
- Upstream num2words is LGPL-2.1-or-later; upstream Misaki's English extra includes
  GPL phonemizer/eSpeak dependencies. They are not customer-profile dependencies.

Do not call the entire resolved environment fully permissive. spaCy transitives
include certifi (MPL-2.0) and tqdm (mixed MPL-2.0/MIT), and NumPy wheels retain
additional bundled-component notices. The full target-platform dependency and
frozen-asset inventories and notices still require review. This source change
makes no new license-risk acceptance or legal assurance.

Primary references:
- https://pypi.org/project/misaki/0.9.4/
- https://github.com/hexgrad/misaki/tree/e820629b96334db28227df37f280e4836d46fadb
- https://github.com/savoirfairelinux/num2words/blob/master/setup.py
- https://github.com/explosion/spacy-models/releases/tag/en_core_web_sm-3.8.0
- https://huggingface.co/hexgrad/Kokoro-82M

## Qualification stages

1. Implementation: explicit English source profile and negative controls exist.
2. Focused qualification: public source tests; exact number-conversion controls;
   actual G2P decimal/currency/override controls with ONNX imports prohibited.
   Historical Kokoro waveform/ASR samples predate the final fixes. The dated
   real source-QA comparisons below establish narrower, current evidence.
   Statistics, ASR and source tests do not replace listening or installed acceptance.
3. Frozen/native packaging: outstanding. Verify both complete file inventory and
   Python archive/module inventory; preserve all model/code/notice identities.
   `scripts/dependencies/verify_customer_speech.py` rejects forbidden dependency
   and payload contamination. It is a separation check, not a completeness or
   whole-graph license attestation. Keep required distribution metadata and the
   spaCy model package/config/tagger data in the frozen bundle.
4. Signed candidate inclusion: outstanding. Tie the reviewed component and
   complete inventory to one exact signed installer/feed artifact through the
   canonical builder, without reusing internal-QA eligibility.
5. Installed speech proof: outstanding. Verify actual Kokoro backend, pronunciation,
   voice pairing, offline cold/warm start, output device and volume controls,
   no hidden fallback, acoustic audibility/intelligibility and failure recovery.

Official eSpeak-NG may be an internal test/reference route only. Neither that
route, source prerequisites, nor a nonempty audio buffer is customer release
or licensing acceptance. No Windows packaging workflow is enabled by this file.

## Actual source-QA evidence, 2026-10-07

These experiments reused the exact canonical model and 54-voice asset bytes in
an owned Linux CPython 3.12.14 environment, with experimental ONNX Runtime 1.30.0.
They do not change frozen Windows pins or activate an application profile.

- English: 52 exact hash-bound wheel dependencies; eight real WAVs over two
  runs. Neutral and currency/initialism `af_heart` component/adapter pairs were
  phoneme- and audio-identical for those two texts only. Quoted standalone
  `'A P I'` instead exposed article `/ɐ/` versus letter `/ˈA/` in both US
  `af_heart` and British `bf_emma`, with distinct audio. British G2P/lexicons were
  verified, not just the voice vector. The existing `in stuh gram` expansion
  was unknown at `stuh` in the component and covered as `stə` by the adapter;
  that comparison was G2P-only, with no dropped phones or fallback. Both routes
  used the same maintained Misaki fork and number converter, not pristine upstream.
- CJK preparation: seven small official wheels, the retained companion, and one
  locally built Jieba 0.42.1 wheel extended the graph to 61 distributions without
  changing the original 52 versions. Jieba's build-only LICENSE/setup.cfg overlay
  restores the exact upstream MIT notice; all 54 original runtime/data files are
  byte-identical, with no native payload or generated dependency edge. This is a
  locally built, notice-restored wheel, not an upstream-published wheel. The
  explicit retained 18-file UniDic dictionary is reused, not downloaded at speech.
- Japanese: native dictionary identity and mixed `こんにちは Hello` G2P passed.
  `こんにちは` with `jf_alpha` then produced component/composed WAVs with
  identical phonemes `koɲɲiʨiβa` and bit-identical audio (0.981 seconds).
- Mandarin: the original combined attempt failed before inference because
  `hao3` became `xau̯↓`, containing U+032F outside the model vocabulary. The
  reviewed Mandarin-only correction restores the U+032F normalization already
  present in retained `ZHG2P.legacy_call`, before the unchanged vocabulary gate.
  The real upstream-output regression fails against baseline; unknown phones,
  other diacritics and empty output still fail. With that reviewed source overlay,
  `你好` with `zf_xiaobei` produced component/composed `ni↓xau↓` and bit-identical
  0.832-second WAVs. Mixed Mandarin/English G2P also passed. The original installed
  companion wheel remained unchanged: corrected-wheel qualification is still open.

All successful runs used one CPU session/thread, pre-import telemetry opt-out,
offline/local assets and no provider credentials. Runs took 8.8–18.0 wall seconds
and remained below 925 MiB peak RSS, inside the 120 CPU/180 wall-second,
4 GiB address/3 GiB RSS/20 MiB output bounds. Python network/process attempts
were guarded; native syscall capture and OS-level no-egress proof were not obtained.

The owner gave positive listening feedback on the delivered English clips and
the exact Japanese `こんにちは` sample. This does not select a winner within
paired English clips or qualify other utterances, voices or languages. Mandarin
listening is still pending. The assistant's own audio-input capability was unavailable.
Four voices across English, Japanese and Mandarin have source-level synthesis
samples; the full eight-family/54-voice matrix remains open. Frozen-app behavior,
complete dependency/native-library notices and customer release eligibility are
separate outstanding gates.

## Native telemetry gate

ONNX Runtime 1.30.0 official privacy documentation states that non-Windows
`ORT_DISABLE_TELEMETRY=1` must be present before runtime initialization to prevent
the uploader, event and persistent-device-ID machinery. Calling the Python
`disable_telemetry_events()` API after import may be too late for initialization
events. Blocking Python sockets does not cover native C++ telemetry.
See https://github.com/microsoft/onnxruntime/blob/v1.30.0/docs/Privacy.md .

This observation is not permission to transmit telemetry or retry a blocked
probe. The dated source-QA runs above used the required flags before Python and
imported the maintained Kokoro guard before ONNX Runtime. Further native runs
must preserve the approved local-file execution and resource conditions. These
runs do not establish application-wide import ordering, OS-level isolation or
frozen Windows acceptance. No actual transmission is asserted without network
evidence.
