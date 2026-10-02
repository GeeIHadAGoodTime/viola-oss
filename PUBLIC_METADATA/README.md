# Source snapshot binding

Current cost-handling source revision: `862f0d1b53d114a7269d8f46b8755296f593151a`.
The manifest binds 2,470 source files; exactly the two listed source/test paths changed
from `1aa40ad76f8176624820ba36f9578d9bc0bb79c8`.

The focused public source contract passes 150 combinations and rejects the prior
outcome-based cost floor. Authoritative carrier facts remain unchanged; existing
measured estimates can still include provider costs. This is not a promise that
every unanswered call is free. Full composed accounting and runtime proof is pending.

Dependency inputs, all six October 1 Windows inventories, and their dated receipts
and advisory files are unchanged. The prior binding is referenced immutably rather
than copied. No new dependency resolution, advisory scan, release or deployment is
claimed here.

From SOURCE, verify with the unchanged canonical tool:

```sh
python -B tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```
