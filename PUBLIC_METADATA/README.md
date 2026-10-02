# Source snapshot binding

Source revision: `a34de7bee7869a3440b97066b85e1f2cdbd89e04`.

This snapshot contains source corrections for the observed Windows native-import
lock inversion, room response parsing, light-theme accent contrast, and missing
Linux/macOS number-normalization dependencies. The Windows access-violation crash
has not been established as the same cause as the native-import deadlock.

The frontend suite passes 981 tests across 137 files and builds. Five focused
unittest methods cover startup ordering and all three desktop dependency profiles.
These are source checks. This revision has not passed installed acceptance and
is not a released product.

Only Linux and macOS requirements add num2words==0.5.14, already pinned on Windows.
The six existing Windows/frontend inventories and dated receipts remain unchanged.
They do not establish Linux/macOS inventory coverage or a fresh advisory scan.

Verify using the unchanged canonical source-binding verifier.
