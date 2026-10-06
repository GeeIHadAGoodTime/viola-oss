// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const postcss = require('postcss');
// Exercise the actual consumer used by the CSS build pipeline.
const postcssRequire = createRequire(require.resolve('postcss'));
const { SourceMapConsumer, SourceNode } = postcssRequire('source-map-js');
const css = 'a { color: red; }';
const basicMap = {
  version: 3,
  sources: ['original.css'],
  sourcesContent: [css],
  names: [],
  mappings: 'AAAA',
};
const indexedMap = (line, column = 0, map = basicMap) => ({
  version: 3,
  sections: [{ offset: { line, column }, map }],
});

describe('source-map-js advisory regression (CVE-2026-93749)', () => {
  // Constructor checks only: never expand or serialize a hostile offset.
  // The predecessor accepts these tiny maps, failing an ordinary assertion.
  it.each([
    ['oversized line', 10_000_001, 0],
    ['infinite line', Infinity, 0],
    ['fractional line', 0.5, 0],
    ['negative line', -1, 0],
    ['infinite column', 0, Infinity],
    ['fractional column', 0, 0.5],
    ['negative column', 0, -1],
  ])('rejects %s before any mapping expansion', (_name, line, column) => {
    expect(() => new SourceMapConsumer(indexedMap(line, column))).toThrow(/Section offset/);
  });

  it('rejects excessive summed offsets in nested sections', () => {
    const nested = indexedMap(6_000_000, 0, indexedMap(6_000_000));
    expect(() => new SourceMapConsumer(nested)).toThrow(/including offsets of nested sections/);
  });

  it('keeps valid indexed mappings and source text intact', () => {
    const consumer = new SourceMapConsumer(indexedMap(1));
    expect(consumer.originalPositionFor({ line: 2, column: 1 })).toEqual({
      source: 'original.css', line: 1, column: 0, name: null,
    });
    expect(consumer.sourceContentFor('original.css')).toBe(css);
    expect(SourceNode.fromStringWithSourceMap('\n' + css, consumer).toString()).toBe('\n' + css);
  });

  it('preserves PostCSS CSS output and an existing source map', () => {
    const result = postcss([]).process(css, {
      from: 'intermediate.css',
      to: 'output.css',
      map: { prev: basicMap, inline: false, annotation: false },
    });
    expect(result.css).toBe(css);
    const consumer = new SourceMapConsumer(result.map.toJSON());
    expect(consumer.originalPositionFor({ line: 1, column: 0 }).source).toBe('original.css');
    expect(consumer.sourceContentFor('original.css')).toBe(css);
  });
});
