import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, cleanup } from '@testing-library/react';
import VolumeControl from './VolumeControl';

// #2772: the volume slider was jumpy — no optimistic local state and no
// throttle, so the thumb re-pinned to the last server `volume` prop on every
// render until the WS echo landed, and a fast drag fired one api.setVolume
// POST per input tick. These tests prove the fix: the thumb tracks the drag
// immediately (no snap-back to a stale server value mid-drag), a fast drag
// emits a bounded number of onVolumeChange commits, and the final dragged
// value is always the one applied.

function getSlider() {
  return screen.getByRole('slider');
}

describe('VolumeControl optimistic state + throttle (#2772)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('tracks a fast drag immediately without snapping back to a stale server volume', () => {
    const onVolumeChange = vi.fn();
    const { rerender } = render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    // Rapid drag: many input ticks in a row, each well inside the throttle
    // window. The thumb must reflect every tick immediately.
    fireEvent.change(getSlider(), { target: { value: '55' } });
    expect(getSlider()).toHaveValue('55');

    // A stale WS echo lands mid-drag (still reporting the pre-drag value).
    // Pre-fix this re-pinned the controlled input back to 50; post-fix the
    // slider is mid-drag and must ignore it.
    rerender(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);
    expect(getSlider()).toHaveValue('55');

    fireEvent.change(getSlider(), { target: { value: '70' } });
    expect(getSlider()).toHaveValue('70');

    fireEvent.change(getSlider(), { target: { value: '90' } });
    expect(getSlider()).toHaveValue('90');
  });

  it('emits a bounded number of onVolumeChange commits for a fast multi-tick drag', () => {
    const onVolumeChange = vi.fn();
    render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    // 20 ticks fired back-to-back (no time advanced between them) simulates
    // a fast drag. Pre-fix this was 20 separate api.setVolume POSTs.
    for (let value = 51; value <= 70; value += 1) {
      fireEvent.change(getSlider(), { target: { value: String(value) } });
    }

    // Nothing has committed yet — still inside the throttle window.
    expect(onVolumeChange).not.toHaveBeenCalled();

    vi.advanceTimersByTime(50);

    // Exactly one trailing-edge commit for the whole burst, carrying the
    // latest (final) value from the burst — not one per tick.
    expect(onVolumeChange).toHaveBeenCalledTimes(1);
    expect(onVolumeChange).toHaveBeenCalledWith(70);
  });

  it('applies the final settled value immediately on drag end, without waiting for the throttle', () => {
    const onVolumeChange = vi.fn();
    render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    fireEvent.change(getSlider(), { target: { value: '60' } });
    fireEvent.change(getSlider(), { target: { value: '65' } });
    // Release before the throttle window elapses.
    fireEvent.pointerUp(getSlider());

    expect(onVolumeChange).toHaveBeenCalledTimes(1);
    expect(onVolumeChange).toHaveBeenCalledWith(65);

    // No further delayed commit fires afterward.
    vi.advanceTimersByTime(200);
    expect(onVolumeChange).toHaveBeenCalledTimes(1);
  });

  it('reconciles with server truth once the drag has settled', () => {
    const onVolumeChange = vi.fn();
    const { rerender } = render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    fireEvent.change(getSlider(), { target: { value: '80' } });
    fireEvent.pointerUp(getSlider());
    expect(onVolumeChange).toHaveBeenCalledWith(80);

    // Server echoes the committed value back.
    rerender(<VolumeControl volume={80} onVolumeChange={onVolumeChange} />);
    expect(getSlider()).toHaveValue('80');

    // A later server-driven change (e.g. another client, a mute toggle)
    // flows straight through since we're no longer mid-drag.
    rerender(<VolumeControl volume={30} onVolumeChange={onVolumeChange} />);
    expect(getSlider()).toHaveValue('30');
  });

  it('releases the drag guard via the idle safety net if pointerup never fires', () => {
    const onVolumeChange = vi.fn();
    const { rerender } = render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    fireEvent.change(getSlider(), { target: { value: '60' } });
    // No pointerup — e.g. the pointer was released outside the window.
    vi.advanceTimersByTime(50);
    expect(onVolumeChange).toHaveBeenCalledWith(60);

    // Idle-release window elapses; the guard clears on its own.
    vi.advanceTimersByTime(300);

    rerender(<VolumeControl volume={40} onVolumeChange={onVolumeChange} />);
    expect(getSlider()).toHaveValue('40');
  });

  it('does not LOSE a server change that arrived while the drag guard was up', () => {
    // The guard was postponing nothing — it was discarding. The reconcile
    // effect is keyed on the `volume` prop, so a value that showed up mid-drag
    // was skipped once and never revisited: the prop already held it, so it
    // could not "change" to it again. The thumb then sat on a number nobody
    // had set and could not correct itself until some third party moved the
    // volume to yet another value. A Playwright probe watched a `/v1/volume`
    // write of 42 leave the thumb on 11 for a full 15 seconds this way.
    const onVolumeChange = vi.fn();
    const { rerender } = render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    fireEvent.change(getSlider(), { target: { value: '11' } });
    vi.advanceTimersByTime(50);            // our own commit goes out
    expect(onVolumeChange).toHaveBeenCalledWith(11);

    // Somebody else sets 42 while we are still inside the drag window.
    rerender(<VolumeControl volume={42} onVolumeChange={onVolumeChange} />);
    expect(getSlider()).toHaveValue('11'); // correct: mid-drag, don't stomp

    // Drag settles. No further prop change will ever arrive — 42 IS the
    // server's value now. Pre-fix the slider stayed on 11 forever.
    // (`act` because the idle-release timer, unlike fireEvent, updates state
    // outside React's own batching.)
    act(() => { vi.advanceTimersByTime(300); });
    expect(getSlider()).toHaveValue('42');
  });

  it('still does not bounce back to a server value older than our own commit', () => {
    // The other half of the pin. "Adopt whatever the server last said when the
    // drag settles" would re-introduce #2772's thumb-snap: the echo of the
    // pre-drag state is not news, it is the state we just overrode.
    const onVolumeChange = vi.fn();
    const { rerender } = render(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);

    // Stale echo of the pre-drag value arrives BEFORE we commit anything.
    rerender(<VolumeControl volume={50} onVolumeChange={onVolumeChange} />);
    fireEvent.change(getSlider(), { target: { value: '80' } });
    vi.advanceTimersByTime(50);
    expect(onVolumeChange).toHaveBeenCalledWith(80);

    // Drag settles with the server still reporting the old 50 (our write has
    // not been echoed yet). The thumb must hold 80, not snap back.
    act(() => { vi.advanceTimersByTime(300); });
    expect(getSlider()).toHaveValue('80');
  });
});
