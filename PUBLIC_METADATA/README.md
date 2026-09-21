# Source snapshot dependency inventories

This metadata accompanies public source revision
`86ece45338d93f4bb1693a220f37188f805663ba`. The canonical manifest contains
2,470 source files and binds the exact `SOURCE/` tree used by the clean Windows
reference installation and public contract qualification.

`windows-desktop.sbom.json` was regenerated from that clean Windows CPython
3.11 environment and its exact pip resolution. The other five inventories are
retained from the preceding qualified source snapshot because their package
inputs were not changed or re-resolved in this qualification. The binding makes
that distinction explicit; it does not claim a fresh optional-package, Linux,
macOS, or frontend dependency resolution.

Keep this directory alongside `SOURCE`. From the `SOURCE` directory, verify the
distributed files and metadata with:

```sh
python -B tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```

| Inventory | Scope | Packages | Status |
|---|---|---:|---|
| `windows-desktop.sbom.json` | Default Windows desktop dependencies | 210 | Refreshed from exact clean install |
| `windows-kokoro.sbom.json` | Desktop plus optional Kokoro speech | 221 | Retained |
| `windows-other-optional.sbom.json` | Desktop plus other optional dependencies | 218 | Retained |
| `windows-all-optional.sbom.json` | Desktop plus all combined optional dependencies | 229 | Retained |
| `windows-deepfilter.sbom.json` | Separate DeepFilter worker environment | 28 | Retained |
| `frontend.sbom.json` | Browser application and development/build dependencies | 431 | Retained; unchanged inputs retested |

The refreshed desktop inventory records the resolved versions, dependency
relationships, publisher license declarations, and the download SHA-256 for
209 remote artifacts. The maintained Pipecat fork was installed from
`SOURCE/third_party/pipecat`, so its component records that relative source path
instead of inventing a wheel hash for the directory install. The pip report and
the installed environment matched on all 210 package names and versions.

The retained inventories preserve their original identities and timestamps.
Their embedded source revision identifies the snapshot where they were
generated; `source-binding.json` records why they remain applicable here.
Python package resolution targets Windows CPython 3.11 AMD64. These reports do
not establish installation results for Linux or macOS.

License declarations are publisher metadata, not a complete license review.
Missing license fields do not grant permission, and the application's
Apache-2.0 license does not relicense dependencies. Preserve the licenses
supplied by installed distributions and built assets. Read `LICENSE`,
`NOTICES.md`, the notices in `LICENSES/`, and the maintained packages'
provenance and license files in the source snapshot.
