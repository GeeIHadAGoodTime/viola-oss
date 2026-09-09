import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';
import PropTypes from 'prop-types';
import { useOptimisticSliderValue } from './useOptimisticSliderValue';

// The hook behind both Rooms-modal sliders (#3003) and the room calibration
// slider. Its component-level behaviour is covered from the user's side in
// components/RoomGroupsModal.test.jsx; this file pins the parts that are hard
// to reach through a rendered slider — what happens on unmount, and what
// happens when the caller's commit callback identity changes mid-drag.

function Harness({ serverValue, commit, options }) {
  const [value, onInput, flush] = useOptimisticSliderValue(serverValue, commit, options);
  Harness.last = { value, onInput, flush };
  return <span data-testid="value">{value}</span>;
}

Harness.propTypes = {
  serverValue: PropTypes.number.isRequired,
  commit: PropTypes.func.isRequired,
  options: PropTypes.object,
};

describe('useOptimisticSliderValue', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Harness.last = null;
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('renders the value the caller passed in before anyone touches it', () => {
    const { getByTestId } = render(<Harness serverValue={7} commit={vi.fn()} />);
    expect(getByTestId('value').textContent).toBe('7');
  });

  it('sends what the user chose even if the slider unmounts inside the debounce window', () => {
    const commit = vi.fn();
    const { unmount } = render(<Harness serverValue={0} commit={commit} />);

    act(() => Harness.last.onInput(42));
    expect(commit).not.toHaveBeenCalled();

    // Modal closed / group collapsed one tick after the drag: the value must
    // not be silently dropped on the floor.
    unmount();

    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledWith(42);

    // ...and the timer it cancelled must not fire a second write later.
    act(() => vi.advanceTimersByTime(500));
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it('calls the newest commit callback, not the one captured when the drag started', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Harness serverValue={0} commit={first} />);

    act(() => Harness.last.onInput(5));
    rerender(<Harness serverValue={0} commit={second} />);
    act(() => vi.advanceTimersByTime(50));

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith(5);
  });

  it('flushing with nothing pending is a no-op', () => {
    const commit = vi.fn();
    render(<Harness serverValue={3} commit={commit} />);

    act(() => Harness.last.flush());
    act(() => Harness.last.flush());

    expect(commit).not.toHaveBeenCalled();
  });

  it('honours a caller-supplied delay and settle window', () => {
    const commit = vi.fn();
    const { getByTestId, rerender } = render(
      <Harness serverValue={0} commit={commit} options={{ delay: 300, settleMs: 1000 }} />
    );

    act(() => Harness.last.onInput(9));
    act(() => vi.advanceTimersByTime(299));
    expect(commit).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(commit).toHaveBeenCalledWith(9);

    // Inside the shorter settle window the server is still ignored...
    rerender(<Harness serverValue={80} commit={commit} options={{ delay: 300, settleMs: 1000 }} />);
    expect(getByTestId('value').textContent).toBe('9');

    // ...and past it, it is heard again.
    act(() => vi.advanceTimersByTime(1000));
    rerender(<Harness serverValue={81} commit={commit} options={{ delay: 300, settleMs: 1000 }} />);
    expect(getByTestId('value').textContent).toBe('81');
  });
});
