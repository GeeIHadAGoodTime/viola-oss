# Public dependency advisory assessment

Source revision: `2d180e2141358433e26ec2c902dd274e050a3ecc`
Source tree SHA-256: `b650f1efba9d54682cb49b6bdc77817797b727df56335a51d9a45e41d9fbd2f4`
Canonical candidate manifest SHA-256: `a85ee64e19f8422b67a777bd6ec30d81f4978af4254cf5d70354c2381ba85f23`
Wheel qualification revision: `4d513b599af5ac7dafc8a1c1ee1d6856344d378f`
Wheel: `pipecat-ai 0.0.108+viola.2`
Wheel SHA-256: `fb0895009eaa6cb49f771e8952347b43dbcd0603d5470d97976e740d2a64f68e`

## aiohttp CVE-2026-69244 floor correction

The upstream [GHSA-cq5v-8q36-5273](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273) identifies `CVE-2026-69244`: aiohttp releases through `3.14.2` are affected and `3.14.3` is the fixed release. The three cross-platform desktop requirement recipes now require `aiohttp>=3.14.3,<4.0.0`, and `tests/public/test_source_contract.py` enforces a patched minimum across Windows, Linux, and macOS.

The retained Windows desktop, Kokoro, other-optional, and all-optional SBOMs each already record the exact resolved component `aiohttp 3.14.3`. Their package identities, hashes, dependencies, and counts therefore remain applicable to this stricter floor and are retained byte-for-byte. No dependency resolution was run for this correction.

The retained OSV scan, dated `2026-09-12T18:33:14.470607+00:00`, covers only the inventories listed in `source-binding.json`. This assessment verifies the patched version already resolved in those inventories and the stricter declared minimum against the primary upstream advisory. No new dependency resolution or broad advisory scan was performed. Other dependency sets require assessment against their own resolved versions. The focused source contract was rerun against this source revision.

## Inventory refresh

The four affected inventories were refreshed from the built wheel metadata. The exact retained pip resolutions were reused; only the Pipecat metadata changed. Local source URLs for retained local distributions were rebased into the disposable candidate tree so the canonical generator could validate their committed source. The receipt records that non-Pipecat package identities and Requires-Dist declarations were unchanged and that no new dependency resolution or advisory scan was performed.
A subsequent manifest-only source commit changed one helper output line and regenerated the canonical LF manifest. The Pipecat dependency, runtime, and test inputs are byte-identical to the wheel qualification revision recorded above, so the wheel was not rebuilt.

| Scope | Components | Refreshed SBOM SHA-256 |
| --- | ---: | --- |
| windows-desktop | 210 | `457d64f0d626ed72f8233dd7cfb85942699ec6b4d3b51d660578c48c678d1a9c` |
| windows-kokoro | 221 | `bc37e0a43edd62ecf95d50f17a13222dde1739557cef15de6d0d718f5a69e75a` |
| windows-other-optional | 218 | `ca3de3770642cbbe8147f733e1a59f920463eab0f0e58c07dcfa24e47bd39aa1` |
| windows-all-optional | 229 | `e268f67db05e11e7d6421eca3f528d8728176010904c1d46eed46a11b104dfdf` |

## Advisory evidence

The existing OSV batch receipt (scanned at `2026-09-12T18:33:14.470607+00:00`) queried `238` unique exact PyPI coordinates and had no service error. The upstream advisory query remains visible as `pipecat-ai 0.0.108`; it is not suppressed by a scan ignore or represented as a false zero. The patched SBOMs preserve the actual fork version `0.0.108+viola.2`.

The two upstream advisory IDs and their source backport evidence are:

- `GHSA-3363-2ph6-35wh` / `PYSEC-2026-2877`: runner `/files` path containment; backported from upstream commit `e780f759d05529c68f7fdfdf50491b1ffca6c984`.
- `GHSA-j8cv-x86q-rj85` / `PYSEC-2026-2878`: runner `/ws` token authentication; backported from upstream merge commit `3032da53434c5ef01d368654b3551cf21c50dec9`.

The source backport mapping is recorded in `SOURCE/third_party/sources.toml`; the vendored runner implementation and its regression test are included in the committed source revision. The prior OSV result therefore remains the authoritative upstream-coordinate advisory evidence, while this qualification proves the maintained fork package identity and source backports. It does not claim the fork was independently rescanned by OSV.

DeepFilter and frontend artifacts were not regenerated because they are unaffected by this dependency-only source fork change; their existing qualification evidence remains separate.

Publication authorization remains a separate release decision.
