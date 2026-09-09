# Maintained speech dependency sources

These are local source forks, not upstream releases. Install from the repository
root with the ordinary requirement files; no private repository or custom package
index is needed. Versions ending `+viola.1` identify the actual modified code.

- Pipecat 0.0.108 (BSD-2-Clause): replace NLTK sentence segmentation with pySBD
  0.3.4 (MIT), remove its corpus download and dependency, and include the existing
  runtime model/audio data in locally built wheels. All other runtime code is
  retained. Direct source and model origins/hashes are recorded in sources.toml
  and pipecat/MODEL_LICENSES. The model files match their publishers byte for byte.
- Kokoro ONNX 0.4.9 (MIT): use an explicitly configured or system-discovered
  eSpeak library/data path, with a clear error when unavailable. Remove the
  espeakng-loader package dependency and import. The phonemizer remains GPL and
  belongs only to the explicit optional Kokoro installation. No eSpeak binary or
  voice data is redistributed by this fork.

The source archives are fixed by SHA-256. Runtime source, package metadata,
README and original license are retained; upstream development environments,
examples and generated distribution metadata are not copied. Text line endings
are normalized to LF. Changes beyond that normalization are reviewable in the
adjacent .patch files. Standalone review comments in those patches identify
removed upstream example credentials; the reconstruction command removes only
those comment lines before applying the patch. Original copyright and license text is preserved.

Verify current source: `python scripts/dependencies/verify_sources.py`.
Reconstruct and compare with public upstream archives and the reviewed patches:
`python scripts/dependencies/verify_sources.py --upstream` (maintenance operation;
requires Git and network access). Installation itself does not need either Git
history or the upstream archives because all build input is already present.

For advisory scans, retain actual local versions in the SBOM and map each fork
explicitly to its original upstream release as an additional advisory lookup.
This prevents an unpublished local version from causing a package to be skipped.
The scanner must still inspect every other exact resolved distribution. NLTK and
the rejected loader must be absent, not ignored or granted an exception.
