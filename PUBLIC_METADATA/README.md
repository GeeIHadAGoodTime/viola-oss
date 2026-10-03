# Source snapshot binding

Source revision: `5ff3e237c745cdfdd21cdbad03639bdb1f49714b`.

EventSource now presents its signed, stream-bound token to both HTTP authentication
layers. Acceptance is limited to the exact GET stream endpoint, while account
cookies and bearer credentials remain authoritative. Loopback desktop tokens
resolve the active desktop principal, matching the existing stream-owner check.
No endpoint authentication exemption or general API-key query fallback is added.

The26-test execution contract module passes, including eight stream-auth tests.
The original source fails the valid-token and actual-route reproduction. The
corrected in-process route returns synthetic data only to its owner and rejects
the wrong account. A full local contract attempt has one environment import
error for missing pipecat-ai package metadata. Canonical CI and installed-product
acceptance remain required. No published customer fix is claimed.

Frontend build-tool dependencies now exclude patch-package and its vulnerable
braces graph. The existing minimatch compatibility patch is preserved by a
bounded postinstall script, with regression checks for idempotence, rejection
of unexpected source, and real glob behavior. Fresh Linux npm ci, 988 frontend
tests, production build and a zero-finding npm audit passed. The frontend
inventory was regenerated from that exact Linux installation; Windows CI
qualification remains required. Five Python inventories remain unchanged.
No installed or published customer repair is claimed for this revision.
