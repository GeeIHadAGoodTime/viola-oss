# Source snapshot dependency inventories

This metadata binds the exact public source tree at source revision
`4501ee07a9e4ecbaa12ffd6b518d58c45d459ed8`, including PyJWT 2.15.1 and the
brace-expansion 5.0.12 frontend override. It contains 2,470 source files.

## Current qualification

The October 1 hosted Windows qualification installed five isolated Python
feature scopes and the exact frontend graph. All installation checks succeeded;
Python package inventories agree with the pip reports and installed versions,
and `pip check` passed. Python report markers record Windows CPython 3.11.9 AMD64.

The public source checks passed 63 Python contract tests. Frontend build and
135 files / 954 tests passed, and npm audit reported zero vulnerabilities.
These are source/synthetic behavior results, not actual provider, carrier,
device, or other-platform runtime acceptance.

| Inventory | Packages | Current status |
|---|---:|---|
| windows-desktop.sbom.json | 210 | Refreshed from clean Windows installation |
| windows-kokoro.sbom.json | 221 | Refreshed from clean Windows installation |
| windows-other-optional.sbom.json | 218 | Refreshed from clean Windows installation |
| windows-all-optional.sbom.json | 229 | Refreshed from clean Windows installation |
| frontend.sbom.json | 431 | Refreshed from exact clean Windows npm installation |
| windows-deepfilter.sbom.json | 28 | Refreshed isolated worker; NumPy 1.26.4, urllib3 2.8.0 |

The first fresh advisory scan caught three urllib3 findings in the old DeepFilter
inventory. Its separate clean Windows refresh now resolves fixed urllib3 2.8.0.
The final scan covers all five freshly installed Python scopes; only the two
upstream Pipecat issues with tested maintained-fork backports remain as raw
version matches. See `advisory-assessment.md` for disposition and scope limits.

## Reproducible evidence

From SOURCE, verify source and inventory binding before installing/building:

```sh
python -B tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```

The checksummed receipts under `qualification-20261001/` retain exact source
hashes, platform markers, installed versions, sanitized package artifact
URLs/hashes, requested extras, dependency declarations, and GitHub run/job
provenance. Local maintained packages use relative source paths instead of
invented download hashes. Package licenses come from publisher metadata; no
permissive license is guessed for an undeclared or ambiguous package.

The generated Python graph evaluates the recorded Windows marker environment
and active requested/transitive extras. Root edges identify requested packages,
including the explicitly installed bootstrap tooling. This differs from a
raw exporter graph that includes inactive extras or an empty pyproject root.

[Source checks](https://github.com/GeeIHadAGoodTime/viola-oss/actions/runs/36895224651)
and [inventory generation](https://github.com/GeeIHadAGoodTime/viola-oss/actions/runs/36895224710)
ran at head `d13c95d87a8d632f5a873ab4dab28736f075455d`, whose runtime SOURCE is
byte-identical to the bound source revision. Metadata refreshes preserve that
source identity. The separate DeepFilter refresh passed in
[run 36897971442](https://github.com/GeeIHadAGoodTime/viola-oss/actions/runs/36897971442).
Seven npm SCP-style VCS references are normalized to equivalent SSH URIs for
CycloneDX schema compliance; the receipt preserves each original URL.
The previous Windows and advisory qualification is retained
as historical evidence in `source-binding.json`.

Public merge/distribution and private composed-consumer qualification remain
separate decisions and checks. This metadata does not change the private source
lock, build or distribute an installer, or establish production readiness.
