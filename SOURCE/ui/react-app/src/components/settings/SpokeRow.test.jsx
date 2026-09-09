import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render } from '../../test/test-utils';
import SpokeRow from './SpokeRow';

describe('SpokeRow', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const spoke = { id: 'kitchen', room_name: 'Kitchen', volume: 40, muted: false, is_hub: false };

  // Regression for #2775: the 5s /api/v1/rooms poll re-renders SpokeRow with
  // a fresh `spoke` prop that can land between an optimistic local volume
  // change and the backend actually committing it, snapping the slider back
  // to the stale value. A settle window after a local change must ignore the
  // poll-driven prop for a bit so the optimistic value sticks.
  it('ignores a poll-driven spoke.volume for a settle window right after a local change', () => {
    const onVolumeChange = vi.fn();
    const { rerender, getByText } = render(
      <SpokeRow spoke={spoke} onVolumeChange={onVolumeChange} onMuteToggle={vi.fn()} />,
    );

    expect(getByText('40')).toBeInTheDocument();

    // Simulate the slider driving a local change straight (bypassing DOM drag
    // math, which Slider.test.jsx already covers) by re-rendering with a
    // handler call equivalent: SpokeRow's own handleVolumeChange is internal,
    // so we drive it through the rendered Slider's track instead.
    const track = document.querySelector('[data-hold-interactive]');
    const originalRect = HTMLElement.prototype.getBoundingClientRect;
    HTMLElement.prototype.getBoundingClientRect = () => (
      { left: 0, right: 100, top: 0, bottom: 44, width: 100, height: 44 }
    );
    fireEvent.mouseDown(track, { clientX: 70 }); // -> optimistic local volume ~70
    HTMLElement.prototype.getBoundingClientRect = originalRect;

    expect(getByText('70')).toBeInTheDocument();

    // The 5s poll now lands with a stale server value (backend hasn't
    // committed the debounced write yet, so /api/v1/rooms still reports
    // something other than the just-set 70) — must not snap the UI back.
    // Using a value distinct from BOTH the original 40 and the new 70 (not
    // just re-sending 40) proves this isn't passing merely because React
    // skipped a same-value effect re-run.
    rerender(<SpokeRow spoke={{ ...spoke, volume: 41 }} onVolumeChange={onVolumeChange} onMuteToggle={vi.fn()} />);
    expect(getByText('70')).toBeInTheDocument();

    // After the settle window elapses, a genuinely new poll value (e.g. the
    // backend's real committed volume, or a change from another client)
    // syncs normally again.
    vi.advanceTimersByTime(3000);
    rerender(<SpokeRow spoke={{ ...spoke, volume: 55 }} onVolumeChange={onVolumeChange} onMuteToggle={vi.fn()} />);
    expect(getByText('55')).toBeInTheDocument();
  });
});
