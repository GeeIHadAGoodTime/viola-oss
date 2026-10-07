# Reconstructed Italian and Hindi source experiments

These are newly written, inactive research modules. They do not recover the lost
October 5 patches or their test receipts. There is no application import, profile
selection, package installation, voice change or native inference in this packet.

Run the complete fresh controls with Python's standard library:

    python -B run_controls.py --output evidence/fresh-controls.json

The runner blocks socket/DNS operations and third-party speech/model imports. The
two modules accept a caller-supplied vocabulary and reject a whole word if any
output symbol is unrepresentable. The tests bind the retained Kokoro configuration
by SHA256; they do not invoke that model. Offsets count original Unicode code
points, not UTF-8 bytes or display graphemes.

The exact MIT Epitran mapping/rule inputs and notices are copied unchanged from
dmort27/epitran commit `3ed96fd7d10f6f5eec75ba1deb10700fcaafe43f` (v1.35.2).
`evidence/upstream-inputs.json` identifies every recovered Git blob and SHA256.
Each converter verifies its rule hashes before use. No Epitran dependency is
imported or added. The new implementations and their limitations are described
inside each prototype root.

The vocabulary fixture comes from GeeIHadAGoodTime/viola-oss commit
`929b17d79b675303bc144ea1d48d0d2b6793f1b3`, path
`SOURCE/third_party/kokoro_onnx/src/kokoro_onnx/config.json`, Git blob
`14a726edd3718279eac426630879ff743955b16a`, SHA256
`5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f`.

This packet has no agreed production repository placement or release binding.
Any later source publication requires an exact reviewed destination and current
repository metadata. Passing these controls does not establish full Italian or
Hindi pronunciation, native audio quality, Windows packaging, installed runtime
behavior, or a ready language route.
