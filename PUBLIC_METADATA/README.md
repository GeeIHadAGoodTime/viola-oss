# Source snapshot binding

Source revision: `a6165b06babf017cb50897a85839abf783958058`.

This source adds a retry control for failed call-history loading and checks the
optional Kokoro tokenizer runtime before reporting the backend available.
It does not install or redistribute a system speech library, change the configured
backend, or claim that a fallback voice has passed installed acceptance.

Frontend: 983 tests in 137 files, build passed, lint has zero errors and 169 warnings.
Three focused Python unittest methods pass, including two new red-to-green
readiness tests. Full Python qualification remains pending canonical Windows CI;
the local minimal environment lacks unrelated desktop test dependencies.

All dependency inputs and retained inventories remain unchanged. No fresh
advisory scan, installed-candidate acceptance, release, or deployment is claimed.
Use the existing canonical source-binding verifier.
