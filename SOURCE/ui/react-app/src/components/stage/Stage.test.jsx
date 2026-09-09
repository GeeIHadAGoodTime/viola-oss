import { useEffect } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../../test/test-utils';
import Stage from './Stage';

function KeepAliveProbe({ name, onUnmount }) {
  useEffect(() => onUnmount, [onUnmount]);
  return (
    <iframe
      title={`${name} iframe`}
      data-testid={`${name}-iframe`}
      src={`https://example.test/${name}`}
    />
  );
}

describe('Stage', () => {
  it('keeps inactive mode content mounted while hiding it', () => {
    const onMusicUnmount = vi.fn();
    const onChatUnmount = vi.fn();
    const { rerender } = render(
      <Stage
        mode="music"
        modeRenderers={{
          music: <KeepAliveProbe name="music" onUnmount={onMusicUnmount} />,
          chat: <KeepAliveProbe name="chat" onUnmount={onChatUnmount} />,
        }}
      />
    );

    const musicFrame = screen.getByTestId('music-iframe');
    expect(screen.getByTestId('stage-mode-panel-music')).toHaveAttribute('aria-hidden', 'false');
    expect(screen.getByTestId('stage-mode-panel-chat')).toHaveAttribute('aria-hidden', 'true');

    rerender(
      <Stage
        mode="chat"
        modeRenderers={{
          music: <KeepAliveProbe name="music" onUnmount={onMusicUnmount} />,
          chat: <KeepAliveProbe name="chat" onUnmount={onChatUnmount} />,
        }}
      />
    );

    expect(screen.getByTestId('music-iframe')).toBe(musicFrame);
    expect(screen.getByTestId('stage-mode-panel-music')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByTestId('stage-mode-panel-chat')).toHaveAttribute('aria-hidden', 'false');
    expect(onMusicUnmount).not.toHaveBeenCalled();
    expect(onChatUnmount).not.toHaveBeenCalled();
  });

  it('renders an explicit placeholder for missing mode renderers', () => {
    render(
      <Stage
        mode="nonsense"
        modeRenderers={{
          music: <div data-testid="music-mode">Music</div>,
        }}
      />
    );

    expect(screen.getByTestId('viola-stage')).toHaveAttribute('data-stage-mode', 'nonsense');
    expect(screen.getByTestId('stage-mode-panel-music')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByTestId('stage-placeholder-nonsense')).toHaveTextContent('Mode not yet built');
  });
});
