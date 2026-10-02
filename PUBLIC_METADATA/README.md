# Source snapshot binding

Current Phone Calling Terms repair source: `3f2822bcc2928ba3f62f12b384d986c78f5c90a0`.
The source manifest binds 2,470 files; exactly seven Phone Terms/CSRF paths changed.
Dependency inputs and all six October 1 clean Windows inventories remain unchanged.

The concise `source-binding.json` records current source hashes, the retained inventory
hashes and an immutable reference to the prior binding. Existing October 1 receipts
and advisory assessment remain dated evidence for their recorded source/environment.
They are not a new dependency resolution, advisory scan or Phone Terms runtime result.

Scoped repair regressions passed 79 backend and 42 frontend cases. Full integrated
runtime, frontend build and installed Windows/customer acceptance are separate gates.
No release or deployment is established by this source binding.

Verify the unchanged canonical binding from SOURCE:

```sh
python -B tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```
