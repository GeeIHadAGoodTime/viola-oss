# Inactive Romance pronunciation qualification

This source-only prototype covers the previously inspected Spanish, French and
Brazilian/European Portuguese rule frontends at upstream commit
`82ee4e7a9b7aded42e0d0d5fd8298b42bfa51a16`. It is not connected to a product
profile, installer, voice selector or production package namespace. The existing
eight product language families and all 54 canonical voices remain unchanged.

Original repository subtree:
https://github.com/ayutaz/piper-plus/tree/82ee4e7a9b7aded42e0d0d5fd8298b42bfa51a16/src/python/g2p

Original license:
https://github.com/ayutaz/piper-plus/blob/82ee4e7a9b7aded42e0d0d5fd8298b42bfa51a16/src/python/g2p/LICENSE

## Provenance and minimal adaptation

The four selected upstream modules use the standard library and their local
`base` module. Their original MIT license is retained verbatim in
`_romance_vendor/LICENSE`. `upstream-bindings.json` records exact original Git and
SHA256 identities. `_romance_vendor/__init__.py` is a new local inert initializer;
it does not copy or execute the upstream registry/encoder initializer.

Three upstream modules have one deliberate semantic change each: their final
unknown-grapheme skip raises `ValueError`. Their modification notices and exact
diff are retained in `upstream-fail-closed.patch`; all other AST semantics are
preserved. This closes actual `qa` → `a` and `piñata` → `piata` omissions in the
French/Portuguese frontends. Known explicit silent-letter rules remain intact.
The original sources remain in the adjacent evidence collection.

## Semantic lowering and coverage

- Treat upstream units as phones, not arbitrary character strings: `rr` is one
  alveolar trill and lowers to IPA `r`; `y_vowel` is the rounded vowel `y`.
- Lower the two affricate units to Kokoro's existing `ʧ` and `ʤ` symbols and
  preserve nasal diacritics. Reject unsupported private-use encodings, dark
  `ɫ`, unknown units and target-vocabulary gaps.
- Retain all upstream prosody fields. Preserve explicit Spanish stress and
  introduce nucleus-anchored primary/secondary stress where supplied by the
  frontend metadata. Reject missing or contradictory stress/nucleus metadata.
- Record every raw source span, including whitespace and French elision,
  alongside normalized frontend tokens and exact model-output ranges. Keep
  whole-phrase French context rather than phonemizing isolated words.
- Fail before G2P on digits, currency, unsupported scripts and controls. This
  prevents loss of values but does not yet provide locale-aware number reading.

## Qualification limits

These controls establish bounded source/phone/prosody preservation. They do not
establish a comprehensive dictionary, phonological correctness or acoustic
quality. Existing upstream examples expose unresolved quality limits: French
`les amis` produces `ləz` rather than its documented `lez`; Portuguese `noite`
uses `i` where the example documents glide `j`. European dark-l cases reject.
Foreign names, mixed scripts, identifiers, numbers, currency and unsupported
punctuation require separate reviewed coverage before product integration.

The target vocabulary is read from the existing reviewed Kokoro configuration;
no model or native inference is loaded. The controls do not install a package,
accept a new agreement, broaden language/voice compatibility, or qualify a
customer Windows dependency graph, frozen bundle, signed candidate or installed
audio. No acoustic claim is made from vocabulary membership alone.
