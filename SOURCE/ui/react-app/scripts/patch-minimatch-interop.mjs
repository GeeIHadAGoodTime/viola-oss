// minimatch 3 expects brace-expansion's old callable export. The security
// override to brace-expansion 5 exposes that same function as `expand`.
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

export function patchMinimatchSource(source, version) {
  if (version !== '3.1.5') throw new Error(`Review minimatch interoperability for version ${version}`);
  const oldLine = "var expand = require('brace-expansion')";
  const newLine = `${oldLine}.expand`;
  const lines = source.split(/\r?\n/);
  if (lines.filter(line => line === newLine).length === 1 && !lines.includes(oldLine)) return source;
  if (lines.filter(line => line === oldLine).length !== 1 || lines.includes(newLine)) {
    throw new Error('Unexpected minimatch source; refusing to apply interoperability patch');
  }
  return source.replace(oldLine, newLine);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const require = createRequire(import.meta.url);
  const path = require.resolve('minimatch');
  const { version } = require('minimatch/package.json');
  const minimatchRequire = createRequire(path);
  if (typeof minimatchRequire('brace-expansion').expand !== 'function') {
    throw new Error('Expected brace-expansion named expand function');
  }
  const source = readFileSync(path, 'utf8');
  const patched = patchMinimatchSource(source, version);
  if (patched !== source) writeFileSync(path, patched);
}
