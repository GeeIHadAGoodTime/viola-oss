# Inactive Italian and Hindi source qualification

This repository placement preserves the twenty-one reviewed reconstruction files
unchanged under `packet/`. Its original README describes the earlier standalone
stage. The placement wrapper is new source; neither converter is imported by an
application, tokenizer factory or active language route.

From `SOURCE`, run:

    python -B qualification/italian_hindi_subsets/run_qualification.py --report ../italian-hindi-report.json

The report must be outside the immutable source tree. The wrapper verifies every
payload and the current Kokoro configuration, copies the packet to a temporary
fixture, then runs the existing 19 methods/412 subtests and 16 assertion-sensitive
mutants. Child processes use UTF-8 explicitly and reject network and third-party
frontend/model imports. Generated evidence stays in the temporary fixture; the
requested external report is written atomically after input-overlap checks.

The retained MIT Epitran rules/notices, original code-point ownership and Kokoro
vocabulary remain exactly bound in `integrity.json`. There is no dependency on
Misaki, the CJK companion, Japanese dictionaries or the pending CJK publication.
No dependency is installed or substituted, and these checks do not run native
speech or model inference.

The converters implement bounded word/representation subsets. Italian lexical
stress, glides and length remain incomplete; Hindi schwa/conjunct and unsupported
features remain incomplete. Recorded rule outputs are not pronunciation goldens.
All eight language families and 54 voices remain required. These experiments do
not qualify an active customer language, acoustic quality, Windows packaging,
signed inclusion or installed speech. The earlier native exit137 cause remains
unresolved and is not retried or explained by this work.
