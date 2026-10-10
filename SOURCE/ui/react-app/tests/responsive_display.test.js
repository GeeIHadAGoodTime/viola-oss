// @vitest-environment node
import { readFileSync } from 'node:fs';
import { URL } from 'node:url';
import { describe, expect, it } from 'vitest';
import postcss from 'postcss';

const css = (name) => postcss.parse(readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8'));
const topbar = css('components/topbar/TopBar.module.css');
const player = css('components/player/PlayerSection.module.css');
const bottom = css('components/voice/BottomRow.module.css');
function atWidth(root, selector, property, width) {
  let value;
  root.walkRules(selector, rule => {
    const parent = rule.parent;
    if (parent.type === 'atrule') {
      const max = parent.params.match(/^\(max-width: (\d+)px\)$/);
      if (!max || width > Number(max[1])) return;
    }
    rule.walkDecls(property, declaration => { value = declaration.value; });
  });
  return value;
}

describe('supported desktop compact layout', () => {
  it.each([1024, 1100, 1279])('keeps clock and player controls compact at %ipx', width => {
    expect(atWidth(topbar, '.clock', 'font-size', width)).toBe('64px');
    expect(atWidth(player, '.trackTitle', 'font-size', width)).toBe('34px');
    expect(atWidth(player, '.transportRow', 'flex-wrap', width)).toBe('wrap');
    expect(atWidth(bottom, '.responseArea', 'font-size', width)).toBe('18px');
  });
  it('preserves the large display and phone tiers', () => {
    expect(atWidth(topbar, '.clock', 'font-size', 1400)).toBe('clamp(76px, 12.5vw, 116px)');
    expect(atWidth(topbar, '.clock', 'font-size', 390)).toBe('50px');
    expect(atWidth(player, '.trackTitle', 'font-size', 390)).toBe('24px');
    expect(atWidth(player, '.section', 'flex-direction', 767)).toBe('column');
  });
  it('centers a fitting panel without centering overflow above the scroll origin', () => {
    expect(atWidth(player, '.trackPanel', 'margin-block', 1024)).toBe('auto');
    expect(atWidth(player, '.trackPanel', 'justify-content', 1024)).not.toBe('center');
    expect(atWidth(player, '.section', 'overflow-y', 1024)).toBe('auto');
    expect(atWidth(player, '.section', 'min-height', 1024)).toBe('0');
  });
});
