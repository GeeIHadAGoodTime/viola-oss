import { describe, expect, it } from 'vitest';
import { createRequire } from 'node:module';
import { patchMinimatchSource } from '../scripts/patch-minimatch-interop.mjs';

const original = "var expand = require('brace-expansion')\n";
describe('security override interoperability', () => {
  it('applies the existing compatibility correction exactly once', () => {
    const patched = patchMinimatchSource(original, '3.1.5');
    expect(patched).toBe("var expand = require('brace-expansion').expand\n");
    expect(patchMinimatchSource(patched, '3.1.5')).toBe(patched);
    expect(patchMinimatchSource(original.replace('\n', '\r\n'), '3.1.5')).toContain('.expand\r\n');
  });
  it('refuses unknown versions and unexpected or duplicate source', () => {
    expect(() => patchMinimatchSource(original, '4.0.0')).toThrow(/Review/);
    expect(() => patchMinimatchSource('unknown', '3.1.5')).toThrow(/Unexpected/);
    expect(() => patchMinimatchSource(original + original, '3.1.5')).toThrow(/Unexpected/);
  });
  it('preserves actual installed minimatch brace, wildcard, and exclusion behavior', () => {
    const require = createRequire(import.meta.url);
    const minimatch = require('minimatch');
    expect(minimatch('src/example.jsx', 'src/*.{js,jsx}')).toBe(true);
    expect(minimatch('src/example.py', 'src/*.{js,jsx}')).toBe(false);
    expect(minimatch('src/nested/example.js', 'src/**/*.js')).toBe(true);
    expect(minimatch('example.js', '!*.py')).toBe(true);
    expect(minimatch.braceExpand('file{1..3}.txt')).toEqual(['file1.txt', 'file2.txt', 'file3.txt']);
  });
});
