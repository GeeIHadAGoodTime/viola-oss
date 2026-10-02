# Source snapshot binding

Chat response recovery source revision: `949a6cef1f8dedba8ed15bf3be6304446eb327b5`.

Only ChatMode and its existing regression test changed from `2a01c2cb3d0158459b76f527d8dd03232b56e44f`.
The correction keeps the visible transport error while a pending assistant result
is not yet in the server snapshot. It does not change authentication or claim
to repair the separate stream-token rejection. Fourteen focused frontend tests
and ESLint pass; the new regression rejects the original behavior. Installed
candidate verification and public delivery remain pending.

Dependency inputs, all six existing Windows inventories and their dated receipts
remain unchanged. No fresh inventory resolution, advisory scan, native runtime
qualification, release or deployment is claimed.

Verify from SOURCE with the unchanged canonical verifier:

```sh
python -B tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```
