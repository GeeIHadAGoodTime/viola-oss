# Source snapshot binding

Source revision: `0b2b61373ca8fdceb859eaac4aa059be2bc3a482`.

Initial HTTP clock synchronization now reads the `/api/v1/clock` endpoint's `hub_time`, retaining `source_time` as a compatibility alias in both flat and wrapped responses. Missing-field diagnostics name both keys.

The manifest binds all 2713 source files. Only `audio_core/streaming/pipeline_wiring.py` changed against protected public `49599a37dc02ab0db194c76a5a328c3770df743f`; all other 2712 source files, the six retained dependency inventories and their original status objects remain unchanged. No new dependency resolution is claimed.

The complete previous binding remains at that immutable protected revision, raw SHA256 `881633cd34b974a074681b8eb199ba7168ea657141c316a1815b3ebcf056cf14`, Git blob `596b4ca181998b04c3f8275c840fcb468bb4a801`. Its native process-runner test records and retained initial Linux tooling failure keep their original source revision `e7e2fea19dd47e5631e09f07ff3ecd854e64dec6` and scope. They do not establish qualification of this clock successor.

Behavioral tests have not yet been executed for this source revision, and exact-head protected public checks have not yet been observed. Combined private/public composition, image qualification, provider acceptance, production deployment and installed-customer acceptance remain separate stages; none is claimed by this metadata preparation.
