# Public dependency advisory assessment

- Source revision: `c7de239c61985273527d297adeef265d82269cb9`
- Source tree SHA-256: `0508733930df331f1a2c38dc299c1ce1d0312356b5a1c58fc7e981eec4b3f3f8`
- Canonical candidate manifest SHA-256: `032d83a5a8ceddaa6bcfa982b51511fdc57efebcca2544e731dd4c6bd80406b9`
- Maintained package: `pipecat-ai 0.0.108+viola.2`

## Exact clean-install scope

The Windows desktop reference environment was resolved and installed afresh on
Windows CPython 3.11. The pip report and installed environment agree on all 210
package names and versions, `pip check` passed, and the refreshed
`windows-desktop.sbom.json` records that exact set. Its SHA-256 is
recorded in `source-binding.json`. The follow-up changes only a platform-specific
test assertion; the installed package inputs and runtime source are unchanged.

The Kokoro, other-optional, all-optional, DeepFilter, and frontend inventories
were not re-resolved. Their input surfaces were unchanged, so their prior
inventories remain separately identified as retained evidence. The frontend's
unchanged inputs were rebuilt and all 954 tests in 135 files passed; npm
reported zero advisories for that installed lockfile scope.

## aiohttp CVE-2026-69244

The primary upstream
[GHSA-cq5v-8q36-5273](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273)
states that aiohttp releases through `3.14.2` are affected and `3.14.3` is the
fixed release. The Windows clean install resolved `aiohttp 3.14.3`, and the
Windows, Linux, and macOS desktop recipes require `aiohttp>=3.14.3,<4.0.0`.
The public source contract enforces that minimum.

## Maintained Pipecat fork

The two relevant primary upstream advisories and the retained backports are:

- `GHSA-3363-2ph6-35wh` / `PYSEC-2026-2877`: runner `/files` path containment,
  backported from upstream commit `e780f759d05529c68f7fdfdf50491b1ffca6c984`.
- `GHSA-j8cv-x86q-rj85` / `PYSEC-2026-2878`: runner `/ws` token authentication,
  backported from upstream merge commit
  `3032da53434c5ef01d368654b3551cf21c50dec9`.

The backport mapping is recorded in `SOURCE/third_party/sources.toml`; the
vendored runner and its regression test are in this exact source tree. The clean
environment installed the maintained package directly from
`SOURCE/third_party/pipecat`, and the public contracts exercised the retained
security assertions.

## Scope and limits

The dated OSV batch receipt from `2026-09-12T18:33:14.470607+00:00` remains
historical evidence for the earlier listed inventories. It queried 238 unique
exact PyPI coordinates without a service error. It was not rerun and is not
presented as a scan of newly released packages after that date. This
qualification refreshed the exact Windows desktop resolution, checked the
current primary advisories above, and reran the source/runtime contracts. It
does not claim live provider, carrier, second-room hardware, Linux, macOS, or
optional dependency acceptance.

Publication authorization remains a separate release decision.
