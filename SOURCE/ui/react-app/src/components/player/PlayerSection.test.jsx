import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import PlayerSection from './PlayerSection';

function StubEmbed() {
  return <iframe title="stub-embed" src="/stub" />;
}

const requiredHandlers = {
  onPlayPause: vi.fn(),
  onNext: vi.fn(),
  onPrevious: vi.fn(),
  onSeek: vi.fn(),
  onVolumeChange: vi.fn(),
  onShuffleToggle: vi.fn(),
  onRepeatCycle: vi.fn(),
  onRating: vi.fn(),
};

function renderPlayerSection(props = {}) {
  render(
    <PlayerSection
      nowPlaying={null}
      isPlaying={false}
      displayProgress={0}
      volume={50}
      shuffleOn={false}
      repeatMode="off"
      YouTubeEmbed={StubEmbed}
      ProviderEmbed={StubEmbed}
      {...requiredHandlers}
      {...props}
    />,
  );
}

// #1504: MusicCommandInput ("Ask Viola to play something") is a mobile/browser
// affordance — the desktop Qt shell already has global type-anywhere capture
// (SmartDisplay.jsx document keydown handler) and showing the box there is
// redundant chrome that collides with the hero surface. Pre-fix, PlayerSection
// rendered the box on every surface whenever a caller passed onSubmitText
// (SmartDisplay.jsx wired it unconditionally) — these two tests prove both
// directions of the fix.
describe('PlayerSection music command input surface gating (#1504)', () => {
  afterEach(() => {
    delete window.viola;
  });

  it('does NOT render the typed music input in the desktop Qt shell', () => {
    // Desktop signal: the Qt bridge object the webview injects (see
    // utils/runtimeSurface.js isDesktopApp()).
    window.viola = { setBrowserOverlayBounds: () => {} };

    renderPlayerSection({ onSubmitText: vi.fn() });

    expect(screen.queryByPlaceholderText('Ask Viola to play something')).toBeNull();
  });

  it('renders the typed music input on the browser/mobile/spoke surface', () => {
    // No Qt bridge -> web client (cloud/LAN browser, or a multiroom spoke,
    // per utils/runtimeSurface.js's isWebClient()).
    expect(window.viola).toBeUndefined();

    renderPlayerSection({ onSubmitText: vi.fn() });

    expect(screen.getByPlaceholderText('Ask Viola to play something')).toBeInTheDocument();
  });

  it('renders nothing when the caller never wires a submit handler, on either surface', () => {
    renderPlayerSection();
    expect(screen.queryByPlaceholderText('Ask Viola to play something')).toBeNull();

    window.viola = { setBrowserOverlayBounds: () => {} };
    renderPlayerSection();
    expect(screen.queryByPlaceholderText('Ask Viola to play something')).toBeNull();
  });
});

// A3.7 (1.0.5 build 5): a long YouTube title wrapped to several lines and
// pushed play/pause below the stage on the Surface. The title is clamped to
// two lines (one on short stages) and the full text stays reachable as the
// element's tooltip.
describe('PlayerSection long titles keep the controls on the stage (A3.7)', () => {
  const longTitle = 'Miles Davis Quintet - Live at the Plugged Nickel 1965 (Complete Second Set, Remastered Edition) [Full Album, Official Audio]';

  it('keeps the full title as a tooltip on the clamped title element', () => {
    renderPlayerSection({ nowPlaying: { id: 't1', title: longTitle, artist: 'Miles Davis' } });
    const title = screen.getByText(longTitle);
    expect(title).toHaveAttribute('title', longTitle);
    expect(screen.getByRole('button', { name: 'Play' })).toBeInTheDocument();
  });

  it('clamps the title in the stylesheet', async () => {
    const { readFileSync } = await import('node:fs');
    const { fileURLToPath } = await import('node:url');
    const { dirname, join } = await import('node:path');
    const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'PlayerSection.module.css'), 'utf8');
    const titleRule = css.match(/\.trackTitle\s*\{[^}]*\}/)[0];
    expect(titleRule).toMatch(/-webkit-line-clamp:\s*2/);
    expect(titleRule).toMatch(/overflow:\s*hidden/);
    expect(css).toMatch(/@media \(max-height: 820px\)\s*\{\s*\.trackTitle\s*\{[^}]*-webkit-line-clamp:\s*1/);
  });
});
