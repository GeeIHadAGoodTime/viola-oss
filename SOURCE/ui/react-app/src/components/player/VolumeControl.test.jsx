import { StrictMode } from 'react';
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

    act(() => { vi.advanceTimersByTime(50); });

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
    act(() => { vi.advanceTimersByTime(200); });
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
    act(() => { vi.advanceTimersByTime(50); });
    expect(onVolumeChange).toHaveBeenCalledWith(60);

    // Idle-release window elapses; the guard clears on its own.
    act(() => { vi.advanceTimersByTime(300); });

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
    act(() => { vi.advanceTimersByTime(50); });            // our own commit goes out
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
    act(() => { vi.advanceTimersByTime(50); });
    expect(onVolumeChange).toHaveBeenCalledWith(80);

    // Drag settles with the server still reporting the old 50 (our write has
    // not been echoed yet). The thumb must hold 80, not snap back.
    act(() => { vi.advanceTimersByTime(300); });
    expect(getSlider()).toHaveValue('80');
  });
});

describe('VolumeControl native acknowledgement ownership', () => {
  const pending = () => {
    let resolve; let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
  };
  const drag = value => {
    fireEvent.change(getSlider(), { target: { value: String(value) } });
    fireEvent.pointerUp(getSlider());
  };
  const accepted = volume => ({ ok: true, volume });
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

  it.each(['pointerUp', 'keyUp', 'blur'])('keeps an acknowledged quick zero after %s', async release => {
    const write = pending();
    render(<VolumeControl volume={60} onVolumeChange={() => write.promise} />);
    fireEvent.change(getSlider(), { target: { value: '0' } });
    fireEvent[release](getSlider());
    expect(getSlider()).toHaveValue('0');
    expect(getSlider()).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('Changing volume…')).toBeInTheDocument();
    await act(async () => write.resolve(accepted(0)));
    expect(getSlider()).toHaveValue('0');
    expect(getSlider()).toHaveAttribute('aria-busy', 'false');
  });

  it('restores confirmed volume after a throttled zero write is refused', async () => {
    const write = pending(); const error = vi.fn();
    render(<VolumeControl volume={60} onVolumeChange={() => write.promise} onVolumeError={error} />);
    fireEvent.change(getSlider(), { target: { value: '0' } });
    await act(async () => vi.advanceTimersByTimeAsync(50));
    fireEvent.pointerUp(getSlider());
    await act(async () => write.reject(new Error('refused')));
    expect(getSlider()).toHaveValue('60');
    expect(error).toHaveBeenCalledTimes(1);
  });

  it('retains a normalized acknowledgement as the next rollback baseline', async () => {
    const write = vi.fn().mockResolvedValueOnce(accepted(25)).mockRejectedValueOnce(new Error('refused'));
    render(<VolumeControl volume={60} onVolumeChange={write} />);
    await act(async () => drag(30));
    expect(getSlider()).toHaveValue('25');
    await act(async () => drag(0));
    expect(getSlider()).toHaveValue('25');
  });

  it('serializes and coalesces newer drag intentions without overwriting them', async () => {
    const first = pending(); const second = pending();
    const write = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<VolumeControl volume={60} onVolumeChange={write} />);
    drag(20); drag(30); drag(40);
    expect(write.mock.calls).toEqual([[20]]);
    expect(getSlider()).toHaveValue('40');
    await act(async () => first.resolve(accepted(18)));
    expect(write.mock.calls).toEqual([[20], [40]]);
    expect(getSlider()).toHaveValue('40');
    await act(async () => second.reject(new Error('refused')));
    expect(getSlider()).toHaveValue('18');
  });

  it.each([{}, undefined, { ok: false, volume: 0 }, { ok: true, volume: NaN },
    { ok: true, volume: Infinity }, { ok: true, volume: -1 }, { ok: true, volume: 101 },
    { ok: true, volume: false }, { ok: 'true', volume: 0 }])('refuses malformed acknowledgement %j', async value => {
    const error = vi.fn();
    render(<VolumeControl volume={60} onVolumeChange={() => Promise.resolve(value)} onVolumeError={error} />);
    await act(async () => drag(0));
    expect(getSlider()).toHaveValue('60');
    expect(error).toHaveBeenCalledTimes(1);
  });

  it('owns a synchronous callback exception', () => {
    const error = vi.fn();
    render(<VolumeControl volume={60} onVolumeChange={() => { throw new Error('refused'); }} onVolumeError={error} />);
    drag(0);
    expect(getSlider()).toHaveValue('60');
    expect(error).toHaveBeenCalledTimes(1);
  });

  it.each(['resolve', 'reject'])('retires timed-out %s and permits a new acknowledgement', async completion => {
    const first = pending(); const second = pending(); const error = vi.fn();
    const write = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<VolumeControl volume={60} onVolumeChange={write} onVolumeError={error} />);
    drag(0);
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(getSlider()).toHaveValue('60');
    expect(screen.getByText('Volume change not confirmed. Check the level and try again.')).toBeInTheDocument();
    drag(30);
    await act(async () => second.resolve(accepted(30)));
    await act(async () => first[completion](completion === 'resolve' ? accepted(0) : new Error('late')));
    expect(getSlider()).toHaveValue('30');
    expect(error).not.toHaveBeenCalled();
  });

  it('checks the response deadline even before a delayed timeout task executes', async () => {
    let now = 0; vi.spyOn(performance, 'now').mockImplementation(() => now);
    const write = pending();
    render(<VolumeControl volume={60} onVolumeChange={() => write.promise} />);
    drag(0); now = 15000;
    await act(async () => write.resolve(accepted(0)));
    expect(getSlider()).toHaveValue('60');
    expect(screen.getByText('Volume change not confirmed. Check the level and try again.')).toBeInTheDocument();
  });

  it.each(['resolve', 'reject'])('drops queued writes and late %s after unmount', async completion => {
    const first = pending(); const error = vi.fn(); const write = vi.fn().mockReturnValue(first.promise);
    const view = render(<VolumeControl volume={60} onVolumeChange={write} onVolumeError={error} />);
    drag(0); drag(30); view.unmount();
    await act(async () => first[completion](completion === 'resolve' ? accepted(0) : new Error('late')));
    expect(write.mock.calls).toEqual([[0]]);
    expect(error).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retains authoritative state received during a request as the rollback baseline', async () => {
    const first = pending(); const write = vi.fn().mockReturnValueOnce(first.promise).mockRejectedValueOnce(new Error('refused'));
    const view = render(<VolumeControl volume={60} onVolumeChange={write} />);
    drag(20);
    view.rerender(<VolumeControl volume={17} onVolumeChange={write} />);
    await act(async () => first.resolve(accepted(20)));
    expect(getSlider()).toHaveValue('17');
    await act(async () => drag(0));
    expect(getSlider()).toHaveValue('17');
  });

  it('keeps a not-yet-committed newer drag when the previous write fails', async () => {
    const first = pending(); const error = vi.fn(); const write = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValueOnce(accepted(30));
    render(<VolumeControl volume={60} onVolumeChange={write} onVolumeError={error} />);
    drag(0);
    fireEvent.change(getSlider(), { target: { value: '30' } });
    await act(async () => first.reject(new Error('earlier refusal')));
    expect(getSlider()).toHaveValue('30');
    expect(error).not.toHaveBeenCalled();
    await act(async () => fireEvent.pointerUp(getSlider()));
    expect(write.mock.calls).toEqual([[0], [30]]);
    expect(getSlider()).toHaveValue('30');
  });
  it('accepts a current request under StrictMode effect replay', async () => {
    const write = vi.fn().mockResolvedValue(accepted(0));
    render(<StrictMode><VolumeControl volume={60} onVolumeChange={write} /></StrictMode>);
    await act(async () => drag(0));
    expect(getSlider()).toHaveValue('0');
    expect(write.mock.calls).toEqual([[0]]);
  });

  it.each(['resolve', 'reject'])('cannot release a newer pending request after a retired %s', async completion => {
    const first = pending(); const second = pending(); const error = vi.fn();
    const write = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<VolumeControl volume={60} onVolumeChange={write} onVolumeError={error} />);
    drag(0);
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    drag(30);
    await act(async () => first[completion](completion === 'resolve' ? accepted(0) : new Error('late')));
    expect(getSlider()).toHaveValue('30');
    expect(getSlider()).toHaveAttribute('aria-busy', 'true');
    expect(error).not.toHaveBeenCalled();
    await act(async () => second.resolve(accepted(30)));
    expect(getSlider()).toHaveValue('30');
    expect(getSlider()).toHaveAttribute('aria-busy', 'false');
  });

});
