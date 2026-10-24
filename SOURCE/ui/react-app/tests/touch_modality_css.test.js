// @vitest-environment node
//
// Touch sizing must not depend on screen width alone. A Surface Pro in tablet
// mode is 1440 CSS px wide in landscape, past every `(max-width: 1023px)`
// "touch devices" block, and QtWebEngine never matches `(pointer: coarse)` on
// Windows. Touch rules therefore key off html[data-input="touch"]
// (utils/inputModality.js). These pin that contract.
import { readFileSync } from 'node:fs';
import { URL } from 'node:url';
import { describe, expect, it } from 'vitest';
import postcss from 'postcss';

const read = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
const css = (name) => postcss.parse(read(name));

function declsFor(root, predicate) {
  const out = {};
  root.walkRules((rule) => {
    if (rule.parent.type === 'atrule') return;
    if (!rule.selectors.some(predicate)) return;
    rule.walkDecls((decl) => { out[decl.prop] = decl.value; });
  });
  return out;
}

const touchSelector = (cls) => (selector) => /html\[data-input=['"]touch['"]\]/.test(selector) && selector.includes(cls);

describe('global touch base (styles/touch-hardening.css)', () => {
  const root = css('styles/touch-hardening.css');

  it('grows small controls to a 44px hit area without moving them', () => {
    const after = declsFor(root, (s) => s.includes('data-input="touch"') && s.endsWith('::after'));
    expect(after.position).toBe('absolute');
    for (const side of ['top', 'bottom', 'left', 'right']) {
      expect(after[side]).toBe('min(0px, calc(50% - 22px))');
    }
  });

  it('keeps every touch rule at zero specificity so component styles win', () => {
    root.walkRules((rule) => {
      if (!rule.selector.includes('data-input')) return;
      for (const selector of rule.selectors) {
        if (selector.includes('.viola-touch-target')) continue;
        expect(selector.startsWith(':where(html[data-input="touch"])'), selector).toBe(true);
      }
    });
  });

  it('stops a dialog scroll from chaining into the stage behind it', () => {
    const decls = declsFor(root, (s) => s.includes('[role="dialog"]'));
    expect(decls['overscroll-behavior']).toBe('contain');
  });

  it('turns off the tap highlight and makes sliders own their drags on touch', () => {
    expect(declsFor(root, (s) => s === 'html')['-webkit-tap-highlight-color']).toBe('transparent');
    const slider = declsFor(root, (s) => s.includes('data-input="touch"') && s.includes('input[type="range"]'));
    expect(slider['touch-action']).toBe('none');
  });
});

describe('component touch sizing applies at any width', () => {
  it.each([
    ['components/player/TransportButton.module.css', '.button'],
    ['components/player/RatingButton.module.css', '.button'],
  ])('%s', (file, cls) => {
    const decls = declsFor(css(file), touchSelector(cls));
    expect(decls['min-width']).toBe('44px');
    expect(decls['min-height']).toBe('44px');
  });

  it('volume slider: 44px drag area, visible thumb, no scroll hijack', () => {
    const root = css('components/player/VolumeControl.module.css');
    const slider = declsFor(root, (s) => touchSelector('.slider')(s) && !s.includes('thumb'));
    expect(slider.height).toBe('44px');
    expect(slider['touch-action']).toBe('none');
    const thumb = declsFor(root, touchSelector('::-webkit-slider-thumb'));
    expect(thumb.opacity).toBe('1');
  });

  it('seek bar: 44px press-and-drag area that does not change layout', () => {
    const track = declsFor(css('components/player/ProgressBar.module.css'), touchSelector('.track'));
    expect(track.height).toBe('44px');
    expect(track.margin).toBe('-20px 0');
    expect(track['background-clip']).toBe('content-box');
  });

  it('agent-browser controls are 44px', () => {
    const decls = declsFor(css('components/stage/modes/browser/BrowserMode.module.css'), touchSelector('.controlButton'));
    expect(decls.width).toBe('44px');
    expect(decls.height).toBe('44px');
  });

  it('hover-only reveals are visible on touch', () => {
    expect(declsFor(css('components/CalendarView.module.css'), touchSelector('.eventActionButton')).opacity).toBe('0.7');
    expect(declsFor(css('components/stage/modes/chat/ChatMode.css'), touchSelector('.chat-message-actions')).opacity).toBe('1');
  });

  it('every 44px chat control in the narrow-screen block has a touch twin', () => {
    const root = css('components/stage/modes/chat/ChatMode.css');
    const narrow = new Set();
    root.walkAtRules('media', (at) => {
      if (at.params !== '(max-width: 1023px)') return;
      at.walkRules((rule) => {
        let sized = false;
        rule.walkDecls((decl) => { if (/44px/.test(decl.value)) sized = true; });
        if (sized) rule.selectors.forEach((s) => narrow.add(s));
      });
    });
    expect(narrow.size).toBeGreaterThan(5);
    const twins = new Set();
    root.walkRules((rule) => {
      if (rule.parent.type === 'atrule') return;
      rule.selectors.forEach((s) => {
        if (s.startsWith('html[data-input="touch"] ')) twins.add(s.slice('html[data-input="touch"] '.length));
      });
    });
    for (const selector of narrow) expect(twins, selector).toContain(selector);
  });
});

describe('modality is installed for the whole app', () => {
  it('main.jsx installs the tracker before rendering', () => {
    const main = read('main.jsx');
    expect(main).toMatch(/installInputModality\(\);[\s\S]*createRoot/);
  });
});
