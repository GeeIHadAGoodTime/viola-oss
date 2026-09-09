# Verify a source snapshot

Use the manifest supplied beside the source snapshot. It identifies every included
file, its hash, and the source revision. Before installing or building into the
folder, verify that its bytes match:

```sh
python tools/release/verify_source.py --manifest ../SOURCE.manifest.json --source .
```

Use the actual supplied manifest filename. The verifier checks for missing,
modified and extra files and compiles Python sources in memory without generating
bytecode. Installation and frontend builds add files, so keep the original source
snapshot for this check and use a separate working copy for development.

After installing the selected requirements and building the frontend:

```sh
python -m unittest discover -s tests/public -v
cd ui/react-app
npm run test:run
cd ../..
```

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
