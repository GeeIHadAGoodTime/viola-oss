import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ProgressBar from './ProgressBar';

// jsdom has no PointerEvent, so fireEvent.pointerDown would drop pointerId,
// pointerType and clientX. A MouseEvent subclass carries them like Chromium.
class TestPointerEvent extends MouseEvent {
  constructor(type, init = {}) {
    super(type, init);
    this._pointerId = init.pointerId ?? 0;
    this._pointerType = init.pointerType ?? 'mouse';
  }

  get pointerId() { return this._pointerId; }

  get pointerType() { return this._pointerType; }
}

let originalPointerEvent;
beforeEach(() => {
  originalPointerEvent = window.PointerEvent;
  window.PointerEvent = TestPointerEvent;
});
afterEach(() => {
  if (originalPointerEvent) window.PointerEvent = originalPointerEvent;
  else delete window.PointerEvent;
});

function setup(props = {}) {
  const onSeek = vi.fn();
  render(<ProgressBar position={30} duration={200} progress={15} onSeek={onSeek} {...props} />);
  const track = screen.getByRole('progressbar', { name: 'Track progress' });
  track.getBoundingClientRect = () => ({ left: 100, top: 0, width: 400, height: 4, right: 500, bottom: 4, x: 100, y: 0 });
  track.setPointerCapture = vi.fn();
  return { onSeek, track };
}

const fill = (track) => track.firstChild.style.width;

describe('ProgressBar touch scrub', () => {
  it('follows the finger while dragging and seeks once, where the finger lifts', () => {
    const { onSeek, track } = setup();
    fireEvent.pointerDown(track, { pointerId: 7, pointerType: 'touch', clientX: 140 });
    expect(fill(track)).toBe('10%');
    fireEvent.pointerMove(track, { pointerId: 7, pointerType: 'touch', clientX: 300 });
    expect(fill(track)).toBe('50%');
    expect(onSeek).not.toHaveBeenCalled();
    fireEvent.pointerUp(track, { pointerId: 7, pointerType: 'touch', clientX: 400 });
    // The click the browser sends after the tap must not seek a second time.
    fireEvent.click(track, { clientX: 400 });
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(150);
    expect(fill(track)).toBe('15%');
  });

  it('a drag the browser cancels seeks nowhere', () => {
    const { onSeek, track } = setup();
    fireEvent.pointerDown(track, { pointerId: 3, pointerType: 'touch', clientX: 140 });
    fireEvent.pointerMove(track, { pointerId: 3, pointerType: 'touch', clientX: 450 });
    fireEvent.pointerCancel(track, { pointerId: 3, pointerType: 'touch' });
    expect(onSeek).not.toHaveBeenCalled();
    expect(fill(track)).toBe('15%');
  });

  it('clamps a drag past either end of the track', () => {
    const { onSeek, track } = setup();
    fireEvent.pointerDown(track, { pointerId: 1, pointerType: 'touch', clientX: 120 });
    fireEvent.pointerUp(track, { pointerId: 1, pointerType: 'touch', clientX: 900 });
    expect(onSeek).toHaveBeenCalledWith(200);
  });

  it('still seeks on a plain click (keyboard / assistive activation)', () => {
    const { onSeek, track } = setup();
    fireEvent.click(track, { clientX: 200 });
    expect(onSeek).toHaveBeenCalledWith(50);
  });

  it('does nothing without a known duration', () => {
    const { onSeek, track } = setup({ duration: 0 });
    fireEvent.pointerDown(track, { pointerId: 1, pointerType: 'touch', clientX: 200 });
    fireEvent.pointerUp(track, { pointerId: 1, pointerType: 'touch', clientX: 200 });
    fireEvent.click(track, { clientX: 200 });
    expect(onSeek).not.toHaveBeenCalled();
  });
});
