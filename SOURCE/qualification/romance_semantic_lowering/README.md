# Inactive Romance source qualification

This directory publishes the independently reviewed, bounded ES/FR/PT semantic-lowering prototype. It does not install, select, import or register a pronunciation backend in the application. No model or package download is involved.

The 13 files under `romance-prototype/` retain the exact bytes of reviewed patch `ac1491f19a3295931bda9cf72cb1649235bf75be742dccbb12a32e89fa63a030`. That historical packet contains task-local fixture paths. Use the portable wrapper rather than running those historical entrypoints in place:

    python SOURCE/qualification/romance_semantic_lowering/run_qualification.py --report /selected/romance-source-report.json

Use an existing Python environment with pytest available. The wrapper verifies the 13 packet payloads, five original upstream code/license files and pinned repository Kokoro vocabulary before staging a temporary fixture. It changes only the temporary provenance record's local file paths. Both test processes disable plugin autoload, use explicit UTF-8, block native/frontend imports and retain their output in the report. All immutable source payloads are checked again after execution. Report destinations within source/packet roots or aliasing their files are rejected before child execution, and allowed external reports are written atomically. No extracted or native model code is run.

`upstream/` contains exact original source and MIT license bytes from ayutaz/piper-plus commit `82ee4e7a9b7aded42e0d0d5fd8298b42bfa51a16`, selected `src/python/g2p` subtree. Git blob and SHA256 bindings are in `integrity.json`; upstream notices, documented modifications and reverse-comparison evidence remain in the historical packet. The three adapted language modules only change the unknown-grapheme skip into a documented error. No other Piper subtree, model or license is adopted by this packet.

Qualification remains bounded: stress and semantic phone lowering, consumed-source traces and rejection of unsupported phones, digits/currency and mixed scripts. French liaison-vowel and Portuguese glide quality witnesses remain recorded by the original qualifier. Broader phonological/acoustic quality, numbers, native packaging and customer eligibility remain open. This packet does not reduce the product's eight language families or change its 54 voices, defaults or existing runtime dependencies. The research alias is not a production packaging declaration.
