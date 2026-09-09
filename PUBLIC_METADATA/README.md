# Source snapshot dependency inventories

These software bills of materials (SBOMs) list the dependencies selected for the
accompanying Viola source snapshot. Keep this directory alongside the `SOURCE`
directory when distributing the snapshot. `source-manifest.json` identifies the
source files by relative path, size and SHA-256 checksum. `source-binding.json`
ties that manifest and its source tree checksum to these six unchanged SBOMs.

From the `SOURCE` directory, verify the distributed source files with:

```sh
python -B tools/release/verify_source.py --manifest ../PUBLIC_METADATA/source-manifest.json --source .
```

| Inventory | Scope | Packages |
|---|---|---:|
| `windows-desktop.sbom.json` | Default Windows desktop dependencies | 210 |
| `windows-kokoro.sbom.json` | Desktop plus optional Kokoro speech | 221 |
| `windows-other-optional.sbom.json` | Desktop plus other optional dependencies | 218 |
| `windows-all-optional.sbom.json` | Desktop plus all combined optional dependencies | 229 |
| `windows-deepfilter.sbom.json` | Separate DeepFilter worker environment | 28 |
| `frontend.sbom.json` | Browser application and development/build dependencies | 431 |

The inventories preserve dependency versions, artifact checksums, dependency
relationships, upstream license declarations, and relative paths for maintained
source packages. Python resolution targets Windows CPython 3.11.9 AMD64. These
reports do not establish installation results for Linux or macOS.

The retained Python resolution reports had been audited by 2026-09-09 UTC
(2026-09-08 in America/Chicago), as recorded in the original audit receipts.
The Python resolution reports do not embed a resolution timestamp. The npm build
inventory records `2026-09-09T00:51:21.732Z` in its metadata. The Python SBOM
metadata timestamp records later assembly of the retained results, not a new
dependency resolution.
Each SBOM retains its original timestamp and identity. The source revision and
checksum inside the Python SBOMs identify the preceding source snapshot; the
separate binding records their applicability to the corrected snapshot after
verifying unchanged dependency recipes, maintained source packages and frontend
inputs. No new dependency resolution, advisory scan or runtime qualification was
performed in preparing these sidecars.

License declarations in an inventory are publisher metadata, not a complete
license review. Missing license fields do not grant permission, and the
application's Apache-2.0 license does not relicense dependencies. Preserve the
licenses supplied by installed distributions and built assets. Read `LICENSE`,
`NOTICES.md`, the notices in `LICENSES/`, and the maintained packages' provenance
and license files in the source snapshot. Custom `FSL-1.1-MIT` labels in the two
Sentry build-tool entries are preserved as license names rather than represented
as SPDX identifiers. Third-party author names and public upstream links remain
part of the inventories.
