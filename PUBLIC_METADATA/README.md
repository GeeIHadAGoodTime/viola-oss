# Source snapshot binding

Source revision: `31e6a87388c224b127f7de68f91fa53437c82555`.

This source preserves the optimistic first chat turn when a new conversation is
created, including send refusal and streaming-transport failure feedback.
The missing-first-message behavior was reproduced in the preceding installed
Linux candidate. This correction does not resolve the separate stream-token
authorization failure and has not yet been verified in an installed candidate.

Frontend: 985 tests in 137 files, build passed, lint has zero errors. Sixteen
focused chat/consent tests pass, including two new red-to-green first-turn cases.
All dependency inputs and retained inventories remain unchanged. No new advisory
scan, release, deployment, or whole-product acceptance is claimed.
