// Advisory gate for the exact frontend lockfile.
//
// `npm audit` has no way to record a reviewed exception, so CI runs this
// instead. It runs `npm audit --json` and fails on every advisory except the
// exceptions below, and an exception only covers the exact installed versions
// it names. A new advisory, a new affected package, or any other installed
// version of an excepted package still fails. Prefer upgrading or overriding
// to a fixed release; add an exception only when no fixed release can be used.
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export const REVIEWED_EXCEPTIONS = [
  {
    advisory: 'GHSA-g3hv-75wg-m249',
    package: 'minimatch',
    versions: ['3.1.5'],
    reason:
      'Dev-only ESLint toolchain (eslint, @eslint/config-array, @eslint/eslintrc, eslint-plugin-react). ' +
      'The only fixed release is minimatch 11.0.1+, whose API (no callable or default export) breaks ' +
      'every one of those dependents, and their latest releases still require minimatch ^3 or ^10. ' +
      'The vulnerable code (firstPhasePreProcess with optimizationLevel >= 2) first shipped in ' +
      'minimatch 7 and does not exist in 3.1.5, and ESLint only matches patterns from our own config. ' +
      'Remove this entry once the ESLint dependents accept a fixed minimatch.',
  },
];

const GHSA = /GHSA(?:-[0-9a-z]{4}){3}/i;

export function advisoryId(via) {
  const match = GHSA.exec(String(via.url || ''));
  return match ? match[0] : String(via.source);
}

function findException(exceptions, id, name, version) {
  return exceptions.find(
    entry =>
      entry.advisory.toLowerCase() === id.toLowerCase() &&
      entry.package === name &&
      entry.versions.includes(version),
  );
}

// Classify every direct advisory finding in an `npm audit --json` report.
// Packages that are only listed because they depend on a vulnerable package
// carry no advisory of their own; they are covered by that package's result.
export function evaluateAudit(report, installedVersion, exceptions = REVIEWED_EXCEPTIONS) {
  if (!report || typeof report !== 'object' || report.error || typeof report.vulnerabilities !== 'object') {
    throw new Error('npm audit did not return a usable report');
  }
  const vulnerabilities = report.vulnerabilities;
  const findings = [];
  for (const [name, vulnerability] of Object.entries(vulnerabilities)) {
    for (const via of vulnerability.via || []) {
      if (typeof via === 'string') {
        if (!(via in vulnerabilities)) throw new Error(`npm audit lists ${name} via unknown package ${via}`);
        continue;
      }
      const id = advisoryId(via);
      const nodes = vulnerability.nodes || [];
      if (nodes.length === 0) throw new Error(`npm audit lists ${id} for ${name} without installed paths`);
      for (const node of nodes) {
        const version = installedVersion(node);
        const exception = findException(exceptions, id, via.name || name, version);
        findings.push({ id, name: via.name || name, node, version, title: via.title, exception });
      }
    }
  }
  const blocked = findings.filter(finding => !finding.exception);
  const unused = exceptions.filter(entry => !findings.some(finding => finding.exception === entry));
  return { findings, blocked, unused };
}

function readInstalledVersion(root) {
  return node => JSON.parse(readFileSync(join(root, node, 'package.json'), 'utf8')).version;
}

function main() {
  const root = process.cwd();
  // A fixed command string: npm is a .cmd shim on Windows and needs a shell.
  const result = spawnSync('npm audit --json', {
    cwd: root,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    shell: true,
  });
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch {
    process.stderr.write(result.stderr || 'npm audit produced no JSON report\n');
    return 1;
  }
  const { findings, blocked, unused } = evaluateAudit(report, readInstalledVersion(root));
  for (const finding of findings) {
    const state = finding.exception ? 'reviewed exception' : 'BLOCKED';
    console.log(`${state}: ${finding.id} ${finding.name}@${finding.version} (${finding.node}) ${finding.title || ''}`);
    if (finding.exception) console.log(`  reason: ${finding.exception.reason}`);
  }
  for (const entry of unused) {
    console.log(`note: exception ${entry.advisory} for ${entry.package} no longer matches any finding; remove it.`);
  }
  if (blocked.length) {
    console.error(`${blocked.length} advisory finding(s) without a reviewed exception. Run \`npm audit\` for details.`);
    return 1;
  }
  console.log(`npm audit gate passed: ${findings.length} finding(s), all reviewed exceptions.`);
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = main();
}
