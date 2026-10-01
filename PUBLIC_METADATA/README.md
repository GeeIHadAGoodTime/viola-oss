# Source snapshot dependency inventories

## Local frontend security follow-up (2026-10-01)

The local candidate adds a narrow brace-expansion 5.0.9 to 5.0.12 override and
lockfile correction on top of the PyJWT review branch. Source revision is
`4501ee07a9e4ecbaa12ffd6b518d58c45d459ed8`. The retained frontend inventory is now stale
as well as the four Windows PyJWT inventories; only DeepFilter inputs remain
unchanged. Fresh Linux frontend install, audit, lint and build passed; this does
not qualify Windows dependencies, hosted checks, or a release. The preceding PyJWT-only candidate did not include this frontend change.

This metadata binds the local unpublished source candidate named above, based on
public `f4d39d1dbb532420e0983051c72f64fb03950278` and review branch head
`2c44d3b24cc1d2c8e80cce7a47af2ffbc7a423ae`. Its dependency pins include PyJWT
2.15.1 and brace-expansion 5.0.12. Prior Windows qualification applies to the
historical source revision recorded in `source-binding.json`, not this candidate.

Five retained inventories are stale and must be refreshed before public merge
or distribution. Fresh Linux frontend tests passed 135 files / 954 tests. No
clean Windows dependency resolution or broad runtime qualification is claimed.

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
| `frontend.sbom.json` | Browser application and development/build dependencies | 431 | Stale: brace-expansion 5.0.9 |

The historical desktop inventory records the resolved versions, dependency
relationships, publisher license declarations, and the download SHA-256 for
209 remote artifacts. The maintained Pipecat fork was installed from
`SOURCE/third_party/pipecat`, so its component records that relative source path
instead of inventing a wheel hash for the directory install. The pip report and
the installed environment matched on all 210 package names and versions.

The retained inventories preserve their original identities and timestamps.
Their embedded source revision identifies the snapshot where they were
generated; `source-binding.json` marks the four PyJWT-bearing inventories
as stale until regenerated against this candidate. The frontend inventory is also
stale after its brace-expansion override changed.
Python package resolution targets Windows CPython 3.11 AMD64. These reports do
not establish installation results for Linux or macOS.

License declarations are publisher metadata, not a complete license review.
Missing license fields do not grant permission, and the application's
Apache-2.0 license does not relicense dependencies. Preserve the licenses
supplied by installed distributions and built assets. Read `LICENSE`,
`NOTICES.md`, the notices in `LICENSES/`, and the maintained packages'
provenance and license files in the source snapshot.
