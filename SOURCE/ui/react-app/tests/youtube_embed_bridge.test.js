import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, URL, URLSearchParams } from 'node:url';
import { describe, expect, it, vi } from 'vitest';

const source = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../ViolaWebsite/js/embed.js'), 'utf8');

function harness({ query = '?v=initial&parent_origin=http%3A%2F%2F192.168.1.10%3A8756', referrer = '' } = {}) {
  const parent = { postMessage: vi.fn() };
  const handlers = {};
  const player = Object.fromEntries(['loadVideoById', 'pauseVideo', 'playVideo', 'seekTo', 'setVolume'].map(name => [name, vi.fn()]));
  player.getCurrentTime = () => 12.5;
  player.getDuration = () => 180;
  const timers = [];
  const clearInterval = vi.fn();
  const overlay = { style: {}, textContent: '' };
  let options;
  const window = {
    parent,
    location: { search: query, origin: 'https://player.example.org' },
    addEventListener: (type, callback) => { handlers[type] = callback; },
  };
  const context = {
    window, URL, URLSearchParams, isFinite,
    document: { referrer, createElement: () => ({}), head: { appendChild: vi.fn() }, getElementById: () => overlay },
    YT: { Player: function (_id, value) { options = value; return player; }, PlayerState: { PLAYING: 1 } },
    setInterval: fn => { timers.push(fn); return timers.length; }, clearInterval, setTimeout: vi.fn(),
  };
  vm.runInNewContext(source, context);
  window.onYouTubeIframeAPIReady();
  return {
    parent, player, options, timers, clearInterval, overlay,
    command: (data, origin = 'http://192.168.1.10:8756', sender = parent) => handlers.message({ data, origin, source: sender }),
  };
}

describe('standalone helper executes the existing video protocol securely', () => {
  it('identifies its own host, preserves muted autoplay and replies to its exact parent', () => {
    const h = harness();
    expect(h.options.playerVars).toMatchObject({ origin: 'https://player.example.org', widget_referrer: 'https://player.example.org', mute: 1, autoplay: 1 });
    h.options.events.onReady();
    expect(h.parent.postMessage).toHaveBeenCalledWith({ type: 'viola_ready' }, 'http://192.168.1.10:8756');
  });

  it('supports track changes, midpoint join, pause/resume, seeking and muted spoke volume', () => {
    const h = harness();
    h.command({ type: 'viola_play', videoId: 'next', startAt: 35 });
    expect(h.player.loadVideoById).toHaveBeenCalledWith({ videoId: 'next', startSeconds: 35 });
    h.command({ type: 'viola_pause' });
    h.command({ type: 'viola_resume' });
    h.command({ type: 'viola_seek', position: 50 });
    h.command({ type: 'viola_volume', level: 0 });
    expect(h.player.pauseVideo).toHaveBeenCalledOnce();
    expect(h.player.playVideo).toHaveBeenCalledOnce();
    expect(h.player.seekTo).toHaveBeenCalledWith(50, true);
    expect(h.player.setVolume).toHaveBeenCalledWith(0);
  });

  it.each([0, -5, '35', NaN, Infinity])('retains plain-load fallback for invalid join position %s', (startAt) => {
    const h = harness();
    h.command({ type: 'viola_play', videoId: 'next', startAt });
    expect(h.player.loadVideoById).toHaveBeenCalledWith('next');
  });

  it('rejects sibling-window and wrong-origin commands', () => {
    const h = harness();
    h.command({ type: 'viola_play', videoId: 'bad' }, 'http://192.168.1.10:8756', {});
    h.command({ type: 'viola_play', videoId: 'bad' }, 'https://evil.example');
    h.command({ type: 'viola_play', videoId: 'bad' }, 'null');
    expect(h.player.loadVideoById).not.toHaveBeenCalled();
  });

  it('preserves older-client referrer compatibility without wildcard replies', () => {
    const h = harness({ query: '?v=initial', referrer: 'http://192.168.1.10:8756/static/react/' });
    h.options.events.onReady();
    h.command({ type: 'viola_pause' });
    expect(h.player.pauseVideo).toHaveBeenCalledOnce();
    expect(h.parent.postMessage).toHaveBeenCalledWith({ type: 'viola_ready' }, 'http://192.168.1.10:8756');
  });

  it.each(['?v=initial', '?v=initial&parent_origin=null', '?v=initial&parent_origin=javascript:bad'])(
    'fails closed without an identified parent: %s', query => {
      const h = harness({ query });
      h.options.events.onReady();
      h.command({ type: 'viola_pause' });
      expect(h.player.pauseVideo).not.toHaveBeenCalled();
      expect(h.parent.postMessage).not.toHaveBeenCalled();
    });

  it('reports position only while playing and preserves ended/error events', () => {
    const h = harness();
    h.options.events.onStateChange({ data: 1 });
    h.timers[0]();
    expect(h.parent.postMessage).toHaveBeenCalledWith({ type: 'viola_position', position: 12.5, duration: 180 }, 'http://192.168.1.10:8756');
    h.options.events.onStateChange({ data: 0 });
    expect(h.clearInterval).toHaveBeenCalledWith(1);
    expect(h.parent.postMessage).toHaveBeenCalledWith({ type: 'viola_state', state: 'ENDED' }, 'http://192.168.1.10:8756');
    h.options.events.onError({ data: 150 });
    expect(h.overlay.style.display).toBe('block');
    expect(h.parent.postMessage).toHaveBeenCalledWith({ type: 'viola_error', code: 150, name: 'EMBED_RESTRICTED' }, 'http://192.168.1.10:8756');
    h.options.events.onError({ data: 153 });
    expect(h.parent.postMessage).toHaveBeenCalledWith({ type: 'viola_error', code: 153, name: 'CLIENT_IDENTITY_REQUIRED' }, 'http://192.168.1.10:8756');
  });
});
