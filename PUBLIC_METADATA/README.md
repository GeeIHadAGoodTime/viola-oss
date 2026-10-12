# Source snapshot binding

Source revision: `e7e2fea19dd47e5631e09f07ff3ecd854e64dec6`.

The maintained process runner accepts optional bytes or text through private file-backed stdin. Existing no-input calls retain their default behavior. Ten native Windows and ten native Linux checks passed, including exact binary/text delivery, fallback cleanup, timeout and normal-exit descendant termination.

The updated manifest binds all 2713 source files. All six retained dependency inventories and their original status objects remain unchanged; product dependency inputs are unchanged. The complete previous source binding remains at protected public revision `3ca6550500de0d78f756729cf42b6dd6953be99a`. The initial underprovisioned Linux tooling failure is retained and accurately scoped in the binding.

Protected source checks, combined private/public composition, image qualification, production deployment and installed-customer acceptance remain separate stages.
