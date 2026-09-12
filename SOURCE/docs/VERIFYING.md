# Verify a source snapshot

Use the manifest supplied beside the source snapshot. It identifies every included
file, its hash, and the source revision. Before installing or building into the
folder, verify that its bytes match:

```sh
python tools/release/verify_source.py --manifest ../PUBLIC_METADATA/source-manifest.json --source .
```

For a distributed candidate, also verify the manifest and all six retained
dependency inventories as one binding:

```sh
python tools/release/verify_source_binding.py --source . --metadata ../PUBLIC_METADATA
```

Release maintainers can regenerate the source manifest after an intentional
source edit with the shipped helper, then review the resulting diff and update
the binding in the same release change:

```sh
python tools/release/create_source_manifest.py --source . --output ../PUBLIC_METADATA/source-manifest.json
```

The helper records the current Git revision when available, hashes every source
file, and writes no dependency or license decisions. A manifest alone is not a
release approval; the six SBOM checksums and binding must still be reviewed.

Use the actual supplied manifest filename. The verifier checks for missing,
modified and extra files and compiles Python sources in memory without generating
bytecode. Installation and frontend builds add files, so keep the original source
snapshot for this check and use a separate working copy for development.

This repository marks `SOURCE/**` and `PUBLIC_METADATA/**` as byte-preserved
paths. Keep that `.gitattributes` file active: with Git's global
`core.autocrlf=true`, text conversion can change already-committed mixed
line-endings and make asset hashes fail even when the working tree was not
intentionally edited.

After installing the selected requirements and building the frontend:

```sh
python -m unittest discover -s tests/public -v
cd ui/react-app
npm run test:run
cd ../..
```

The contributor CI runs the same source-binding, Python-contract, and frontend
checks on GitHub-hosted Windows runners with Python 3.11. It sets `CI=1` and
does not provide Sentry or provider credentials, so these checks cannot upload
source maps or contact a customer integration. The frontend audit reads the
exact `ui/react-app/package-lock.json` selected by `npm ci`; the five Python
SBOMs similarly identify their exact resolved versions. A clean CI result is
source and test evidence, not proof of live provider, carrier, hardware, or
other-platform behavior.

The `tests/public` command contains two kinds of evidence: source-boundary
contracts for the shipped packet and synthetic Python behavior checks (including
safety and phone behavior). Both are useful checks, but neither is provider,
carrier, startup, UI, or live runtime acceptance. The frontend Vitest suite is
separate evidence; report its exact command and receipt when it is run. The
frontend command requires the dependencies installed by `npm ci` in
`ui/react-app`. Do not infer a test count or passing result from file counts,
source presence, or an unrun command.

Dependency verification must use the selected installation, not another machine's
global packages. A release's software bill of materials (SBOM) describes exact
resolved package versions and dependency edges, accompanied by source/artifact
hashes and the platform/Python/feature scope. Default desktop and optional Kokoro
are different dependency scopes. The release security report must include all
selected transitive dependencies and any reviewed scanner findings.

Runtime acceptance includes real local/BYOK provider behavior, UI and source startup,
local state persistence, speech, tool execution, and self-hosted phone behavior.
Mocked provider tests cover error/control paths but are not proof of real model
inference or carrier calls. Use synthetic inputs and your own provider accounts
for the latter; never use production/customer data as a fixture.
