# Source snapshot dependency inventories

This metadata accompanies a local, unpublished PyJWT security update with source
commit `f8d5457278380af4c31d5e1573eb0c00432dcdd3` based on public
`f4d39d1dbb532420e0983051c72f64fb03950278`. The canonical manifest binds
the exact local `SOURCE/` tree. Its requirement pins change to PyJWT 2.15.1.
The prior clean Windows installation and public contract qualification apply to
the earlier source revision recorded in `source-binding.json`, not this candidate.

Four retained Windows inventories still list PyJWT 2.13.0, so they are stale for
this candidate and must be regenerated from clean installations before public
merge or distribution. The separate DeepFilter and frontend inventories have
unchanged dependency inputs. No clean dependency resolution or broad runtime
qualification of this candidate is claimed.

Keep this directory alongside `SOURCE`. From the `SOURCE` directory, verify the
distributed files and metadata with:

```sh
python -B tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```

| Inventory | Scope | Packages | Status |
|---|---|---:|---|
| `windows-desktop.sbom.json` | Default Windows desktop dependencies | 210 | Stale: PyJWT 2.13.0 |
| `windows-kokoro.sbom.json` | Desktop plus optional Kokoro speech | 221 | Stale: PyJWT 2.13.0 |
| `windows-other-optional.sbom.json` | Desktop plus other optional dependencies | 218 | Stale: PyJWT 2.13.0 |
| `windows-all-optional.sbom.json` | Desktop plus all combined optional dependencies | 229 | Stale: PyJWT 2.13.0 |
| `windows-deepfilter.sbom.json` | Separate DeepFilter worker environment | 28 | Retained; unchanged inputs |
| `frontend.sbom.json` | Browser application and development/build dependencies | 431 | Retained; unchanged inputs |

The historical desktop inventory records the resolved versions, dependency
relationships, publisher license declarations, and the download SHA-256 for
209 remote artifacts. The maintained Pipecat fork was installed from
`SOURCE/third_party/pipecat`, so its component records that relative source path
instead of inventing a wheel hash for the directory install. The pip report and
the installed environment matched on all 210 package names and versions.

The retained inventories preserve their original identities and timestamps.
Their embedded source revision identifies the snapshot where they were
generated; `source-binding.json` marks the four PyJWT-bearing inventories
as stale until regenerated against this candidate.
Python package resolution targets Windows CPython 3.11 AMD64. These reports do
not establish installation results for Linux or macOS.

License declarations are publisher metadata, not a complete license review.
Missing license fields do not grant permission, and the application's
Apache-2.0 license does not relicense dependencies. Preserve the licenses
supplied by installed distributions and built assets. Read `LICENSE`,
`NOTICES.md`, the notices in `LICENSES/`, and the maintained packages'
provenance and license files in the source snapshot.
