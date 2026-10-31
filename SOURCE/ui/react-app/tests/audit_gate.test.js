import { describe, expect, it } from 'vitest';
import { REVIEWED_EXCEPTIONS, evaluateAudit } from '../scripts/audit-gate.mjs';

const minimatchAdvisory = {
  source: 1244012,
  name: 'minimatch',
  title: 'minimatch has potential denial of service via globparts amplification',
  url: 'https://github.com/advisories/GHSA-g3hv-75wg-m249',
  range: '<11.0.1',
};

function report(vulnerabilities) {
  return { auditReportVersion: 2, vulnerabilities, metadata: {} };
}

const eslintChain = {
  minimatch: { name: 'minimatch', via: [minimatchAdvisory], nodes: ['node_modules/minimatch'] },
  eslint: { name: 'eslint', via: ['minimatch'], nodes: ['node_modules/eslint'] },
  'eslint-plugin-react': { name: 'eslint-plugin-react', via: ['minimatch'], nodes: ['node_modules/eslint-plugin-react'] },
};

const versions = paths => node => {
  if (!(node in paths)) throw new Error(`unexpected node ${node}`);
  return paths[node];
};

describe('npm audit gate', () => {
  it('admits the reviewed minimatch advisory only for the exact excepted version', () => {
    const result = evaluateAudit(report(eslintChain), versions({ 'node_modules/minimatch': '3.1.5' }));
    expect(result.blocked).toEqual([]);
    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].exception).toBe(REVIEWED_EXCEPTIONS[0]);
    expect(result.unused).toEqual([]);
  });

  it('blocks the same advisory on any other installed version or path', () => {
    const other = evaluateAudit(report(eslintChain), versions({ 'node_modules/minimatch': '10.2.6' }));
    expect(other.blocked.map(finding => finding.version)).toEqual(['10.2.6']);

    const nested = report({
      ...eslintChain,
      minimatch: {
        ...eslintChain.minimatch,
        nodes: ['node_modules/minimatch', 'node_modules/glob/node_modules/minimatch'],
      },
    });
    const result = evaluateAudit(
      nested,
      versions({ 'node_modules/minimatch': '3.1.5', 'node_modules/glob/node_modules/minimatch': '10.2.6' }),
    );
    expect(result.blocked.map(finding => finding.node)).toEqual(['node_modules/glob/node_modules/minimatch']);
  });

  it('blocks every advisory without a reviewed exception', () => {
    const glob = {
      glob: {
        name: 'glob',
        via: [{ source: 1, name: 'glob', title: 'Glob', url: 'https://github.com/advisories/GHSA-3g3m-2fjr-6pw4' }],
        nodes: ['node_modules/glob'],
      },
    };
    const result = evaluateAudit(report(glob), versions({ 'node_modules/glob': '13.0.6' }));
    expect(result.blocked.map(finding => finding.id)).toEqual(['GHSA-3g3m-2fjr-6pw4']);
  });

  it('reports an exception that no longer matches without failing', () => {
    const result = evaluateAudit(report({}), versions({}));
    expect(result.blocked).toEqual([]);
    expect(result.unused).toEqual(REVIEWED_EXCEPTIONS);
  });

  it('fails closed on an unusable or inconsistent report', () => {
    expect(() => evaluateAudit(undefined, versions({}))).toThrow(/usable report/);
    expect(() => evaluateAudit({ error: { code: 'ENOAUDIT' } }, versions({}))).toThrow(/usable report/);
    expect(() =>
      evaluateAudit(report({ eslint: { name: 'eslint', via: ['minimatch'], nodes: ['node_modules/eslint'] } }), versions({})),
    ).toThrow(/unknown package/);
    expect(() =>
      evaluateAudit(report({ minimatch: { name: 'minimatch', via: [minimatchAdvisory], nodes: [] } }), versions({})),
    ).toThrow(/without installed paths/);
  });

  it('keeps every exception explained and pinned to exact versions', () => {
    for (const entry of REVIEWED_EXCEPTIONS) {
      expect(entry.advisory).toMatch(/^GHSA(-[0-9a-z]{4}){3}$/);
      expect(entry.versions.length).toBeGreaterThan(0);
      for (const version of entry.versions) expect(version).toMatch(/^\d+\.\d+\.\d+$/);
      expect(entry.reason.length).toBeGreaterThan(40);
    }
  });
});
