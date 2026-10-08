# Customer speech component qualification

Status: first-release qualification targets English, Spanish and Mandarin. Existing language routes and the canonical 54-voice asset are retained. Selected-voice source wiring is integrated; frozen and installed acceptance remain open.

## Recovered decision and scope

The historical `docs/TTS_ENGINE_RESEARCH.md` in the private service repository
records Kokoro ONNX with Misaki English and no eSpeak fallback. Its claim that
all dependencies were permissive and the path was already complete was not
supported by the actual tokenizer, which used phonemizer and eSpeak. The same
historical document also describes 48 voices/eight offline languages. The
current phone source preserves eight language families. The owner's 2026-10-07
launch decision prioritizes three to five languages used in the United States;
the selected first-release set is **English, Spanish and Mandarin**. Mandarin
does not imply Cantonese or every language grouped as Chinese. Other retained
routes, including Japanese, Italian and Hindi, are deferred expansion work.
The canonical 54-voice asset stays intact; the historical 48-voice sentence is
not a current voice inventory or an acceptance claim.

The default customer component is English. The explicitly constructed, inactive
CJK companion now has the bounded source evidence below. Customer activation
requires qualification of the selected launch routes and voices, including
their exact target dependencies, notices and installed behavior. Unselected
languages and all 54 voices are not blanket release prerequisites. Desktop and local phone source now share the explicitly selected
English/Spanish/Mandarin composition in a marked qualification artifact.
Source routing controls alone do not qualify its installed behavior.

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
- The existing explicit composition can add requested `es`, `fr` and `pt-br`
  routes from `voice/customer_romance/`. These are the retained qualification
  components with ordinary package imports, unchanged pronunciation algorithms
  and original MIT notices. CJK dependencies are checked when a CJK route is
  requested; constructing English/Romance alone does not admit or initialize
  CJK. Unsupported words, numbers, currency, scripts and phones still reject
  the complete input. Bare phone locale `pt` and European `pt-pt` are not
  silently mapped to Brazilian Portuguese. Italian/Hindi remain separate
  single-word components until an explicit application text contract is qualified.
- Existing public number formatting now uses `voice/english_numbers.py` rather
  than importing LGPL num2words. Its bounded cardinal/ordinal/year contract is
  checked against the former implementation using a test-only reference.
- `requirements_customer_speech_mandarin.txt` explicitly adds the `.4`
  companion's Mandarin extra to the retained English input. Spanish uses the
  maintained source component. Japanese has a separate extra and still requires
  its explicit dictionary when selected; its native/dictionary dependencies
  do not gate the three-language launch candidate. Pronunciation/data/notice
  bytes are unchanged by this dependency split.

Voice selection must be explicit and match the selected locale. The first
bounded qualification pass uses `af_heart`, `ef_dora` and `zf_xiaobei`, reusing
their retained samples. That is test sequencing, not a permanent product limit.
Other existing English, Spanish and Mandarin voice choices remain eligible for
qualification; their presence in the canonical asset alone does not make them
advertised or accepted. Application voice/locale mismatch handling and frozen
selected-voice proof remain separate requirements.

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
  companion wheel remained unchanged in that source-overlay run. A subsequent
  offline build installed the corrected `.2` artifact into an isolated target;
  three real G2P regressions and one composed `你好` synthesis passed through
  that installed artifact. Its waveform was bit-identical to the earlier source
  run. That experiment explicitly retained two physical `.2` identities and
  did not claim clean final packaging.
- Corrected CJK packaging: version `0.9.4+viola.cjk.3` now identifies the corrected
  runtime uniquely. Its offline-built wheel is 511,487 bytes, SHA-256
  `3e4c56598eec635d92aaa79c1c42fd396ba75e61c262744d163d098432fdd238`.
  All runtime, data and notice bytes match the corrected `.2` artifact; only
  version/readme metadata and RECORD changed. A fresh owned target combines
  real installations of the retained English wheel and `.3` with 59 unchanged
  shared dependency installations. The selected graph contains one companion
  identity and preserves the other 60 versions. Three real Mandarin G2P tests
  passed through that installed `.3` package with ONNX unavailable and no CJK
  source overlay. This is a Linux source-QA
  packaging step, not a Windows/frozen bundle or registry publication.
- Launch-subset dependency separation: companion `0.9.4+viola.cjk.4` gives the
  changed dependency metadata a new identity. Its pure wheel is 511,838 bytes,
  SHA-256 `e9688b42b822455bf80a89a2dc2526add725867a2b0f5182bcf4f03a7f93284f`.
  Two offline builds match exactly. All 16 runtime/data/embedded-notice entries
  match the retained `.3` source, and all seven notice paths and wheel RECORD
  entries verify. Mandarin and Japanese extras retain their existing exact
  pins; Mandarin installation/admission no longer requires Japanese inputs.
  Twenty-two dependency controls and the updated 36-case inert source replay
  pass. This establishes metadata/source behavior, not a new target-platform
  installation, waveform, frozen artifact or customer release acceptance.
- Selected application voice routing now checks named voices against their
  pronunciation locale before tokenization or synthesis. Phone language changes
  retain a compatible voice or choose the matching existing default, and failed
  changes restore both fields. Explicit Mandarin aliases do not admit Cantonese
  or unspecified regional Chinese variants. The implicit English adapter keeps
  its previous style compatibility; dormant language routes and assets remain.
  These source changes have new package identities: `kokoro-onnx 0.4.9+viola.3`
  (18,890 bytes, SHA-256
  `2fedd09693672da46da0e2ee4dee3265bc24607774699bf6c68d8eec299f9b33`)
  and `pipecat-ai 0.0.108+viola.3` (10,752,703 bytes, SHA-256
  `a09f12dffc545ca0cac022fdb5ff441e9298556be8f26d23e7a40f5ac5a6fb80`).
  Both wheels reproduce exactly from the retained build backends. Pipecat's
  Kokoro extra pins this maintained Kokoro identity; its existing
  `onnxruntime~=1.23.2` dependency is unchanged. The separate Linux 1.30.0
  investigation does not replace that Windows compatibility declaration.
  Source checks and wheel contents do not establish installed application,
  acoustic, native-library closure or customer release acceptance.
- Romance: the retained ES `pero`/`perro`, FR `tu`/`tout`, and PT-BR
  `bom dia`/`noite` components produced six finite, non-silent WAVs using
  `ef_dora`, `ff_siwis` and `pf_dora`. These runs supplied precomputed phones
  with `is_phonemes=True`; they do not prove original-text application dispatch.
  The existing Portuguese `noite` `/i/` versus documented `/j/` witness remains
  open. French liaison, numbers, mixed text and broader linguistic coverage
  are not qualified by the two French words. A subsequent reviewed composition
  run supplied the same six original texts to both Kokoro `create` and
  `create_stream`, with `is_phonemes=False`, the exact bound owner and caller
  voices. All twelve invocations passed; each stream, complete call and retained
  component produced bit-identical float PCM. No samples exceeded ±1. One
  shared session took 13.49 wall seconds and about 661 MiB observed RSS. This
  proves these bounded text routes through the wrapper; it does not prove a
  full desktop/phone process, audio device, phone dialect mapping or acoustics.
- Italian/Hindi: the existing single-word components produced six real WAVs:
  Italian `pèsca`, `pésca`, `scìa` with `if_sara`, and Hindi `किताब`, `माँ`,
  `छाता` with `hf_alpha`. The two Italian stress/vowel contrasts produced
  distinct phones and float PCM. Italian float output exceeded ±1 in 84, 35
  and 14 samples respectively; PCM16 export clipped those samples. Hindi had
  no over-range samples. No repair or listening judgment is inferred. These
  runs also used precomputed phones. Italian still requires exactly one
  explicit accent in one word; Hindi retains its bounded Devanagari word,
  schwa, conjunct and unsupported-feature limits. Neither component provides
  a general sentence, punctuation, number or mixed-language contract.

All successful runs used one CPU session/thread, pre-import telemetry opt-out,
offline/local assets and no provider credentials. Runs took 8.8–18.0 wall seconds
and remained below 925 MiB peak RSS, inside the 120 CPU/180 wall-second,
4 GiB address/3 GiB RSS/20 MiB output bounds. Python network/process attempts
were guarded; native syscall capture and OS-level no-egress proof were not obtained.

The owner gave positive listening feedback on the delivered English clips and
the exact Japanese `こんにちは` sample. This does not select a winner within
paired English clips or qualify other utterances, voices or languages. Mandarin
listening is still pending. The assistant's own audio-input capability was unavailable.
Nine distinct voices across the retained eight families have bounded component
synthesis samples. The wider matrix remains expansion evidence; first-release
acceptance concerns the selected English/Spanish/Mandarin voices and input
coverage. Spanish listening remains unverified. Other Romance and Italian/Hindi
listening and general application text work are deferred. Complete desktop/phone
integration remains open for the launch subset.
Frozen-app behavior,
complete dependency/native-library notices and customer release eligibility are
separate outstanding gates.

### Normal qualification launch and installed retest

The marked Windows qualification artifact selects its local pronunciation route
through a frozen startup hook before application/native imports. Normal desktop
command and wake/PTT callers share the composed English/Spanish/Mandarin owner;
normal phone callers use that composition with matching named voices. Output
language and voice are separate from speech-recognition and answer-context
settings. Failed selection or missing inputs leave speech unavailable rather
than selecting eSpeak, system speech or remote TTS. These are source-wiring
controls; they do not establish installed capture, synthesis or listening.

An internal #1482 hardware observation on 2026-10-07 reported no wake-word
response. A held microphone-button attempt appeared to listen, then returned
an account-required message. This proves attempted input only; capture and
transcription were not confirmed. The installed retest must separately check
wake initialization and microphone/capture behavior, then repeat the authenticated
PTT path. An account gate does not explain or qualify the wake initialization
failure. Neither observation validates the customer pronunciation artifact.

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

## Selected Mandarin data and recovered notices, 2026-10-08

The following are exact input identities, separate from Windows frozen inclusion.
They were read from the selected official PyPI artifacts and checked against
PyPI SHA-256 metadata; both wheel RECORD inventories were checked. No model,
voice asset, pronunciation algorithm or dependency version changed.

- `pypinyin-0.55.0-py2.py3-none-any.whl`: 840,203 bytes, SHA-256
  `d53b1e8ad2cdb815fb2cb604ed3123372f5a28c6f447571244aca36fc62a286f`.
  `pypinyin/pinyin_dict.json` is 788,780 bytes, SHA-256
  `5f294c01e6c6c0a1c8e329c79335a3f8e0b27d06bf1de7a99244b765892d1e5b`;
  `pypinyin/phrases_dict.json` is 2,545,585 bytes, SHA-256
  `a45ff140a6b631ca9c82127b280a2f414e0aba6bb2824a0e9d1e77fff359c665`.
  Its package MIT text is at `pypinyin-0.55.0.dist-info/licenses/LICENSE.txt`,
  not directly in the dist-info root.
- The [pypinyin v0.55.0 release](https://github.com/mozillazg/python-pinyin/tree/df101577145af2eb1abe5656e592e34e3bb56d23)
  selects [pinyin-data fa9761ff](https://github.com/mozillazg/pinyin-data/tree/fa9761fff402f8560196b1ba085c437c52b56d7c)
  and [phrase-pinyin-data cee0ed6e](https://github.com/mozillazg/phrase-pinyin-data/tree/cee0ed6e6e4898580cafd2bd5e3723e20b214aa0).
  Replaying its documented transformations from those two exact `pinyin.txt`
  files reproduces both packaged JSON dictionaries byte-for-byte: 41,923
  character entries and 47,111 phrase entries. The two upstream MIT notices
  are retained as `LICENSES/pinyin-data-MIT.txt` and
  `LICENSES/phrase-pinyin-data-MIT.txt`. The selected phrase generator consumes
  `pinyin.txt`; it does not consume the separately provided `large_pinyin.txt`
  or `cc_cedict.txt`. The character source records Unicode 16.0.0 Unihan
  inputs; final notices must preserve the applicable Unicode data attribution
  as well as the immediate package notices. This record does not assert
  upstream data licensing closure solely from package MIT metadata.
- `jieba-0.42.1.tar.gz`: 19,214,172 bytes, SHA-256
  `055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2`.
  `jieba/dict.txt` is 5,071,852 bytes, SHA-256
  `7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8`.
  Its Git blob `fc6075f64943e1861c420db4da38063de9d8afc5` matches the
  [official release source](https://github.com/fxsjy/jieba/tree/1e20c89b66f56c9301b0feed211733ffaa1bd72a).
  The archive omits the release MIT text; that exact 1,075-byte text is now
  retained as `LICENSES/jieba-0.42.1-MIT.txt` (SHA-256
  `18ba0984839f85853b29fadaf992f7dba8fd0ca0fbeae34de2b8735222dc7a37`).
  This does not claim a new built or installed Jieba wheel.
- `ordered_set-4.1.0-py3-none-any.whl`: 7,634 bytes, SHA-256
  `046e1132c71fcf3330438a539928932caf51ddbc582496833e23de611de14562`.
  Its sole runtime module exactly matches
  [release d921651b](https://github.com/rspeer/ordered-set/tree/d921651b2737f5cfe711868fab558a8ef79e26ca);
  its omitted MIT text is retained in
  `LICENSES/CUSTOMER_SPEECH_SUPPLEMENTAL_NOTICES.txt`. That supplement also
  preserves previously reviewed FlatBuffers 25.12.19 and srsly 2.5.4 vendored
  ruamel.yaml notices; those two historical Linux artifact associations do
  not establish the final Windows versions or native inventory.

These hashes and notice bytes can now be bound by the existing private
qualification preparer. A nonempty dictionary check is insufficient to prove
these identities. The current public-source addition does not modify that
private preparer or assert that the notices are already collected into an
installer. The remaining artifact work is exact Windows resolved-wheel and
native-library closure, physical files plus embedded Python inventory,
accessible notices and any applicable source obligations, signed publisher
binding, and installed selected-language/voice listening and failure recovery.
The existing qualification build must produce that evidence; it does not
require installed proof before it may be built.
