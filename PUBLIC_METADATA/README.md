# Source snapshot binding

Source revision: `c2a76353e29c362b489df1efc9f24a0e58c86b3d`.

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

Frontend and dependency inputs, six retained inventories and prior receipts are
unchanged. No fresh dependency resolution or vulnerability scan is claimed.
