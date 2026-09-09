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
