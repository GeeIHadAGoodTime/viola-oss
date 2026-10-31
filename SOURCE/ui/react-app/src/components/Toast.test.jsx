import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import ToastContainer from './Toast';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('Toast durationMs (1.0.5 build 7, A11.5)', () => {
  it('keeps a toast with durationMs up past the default and then dismisses it', () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(
      <ToastContainer
        toasts={[{ id: 1, message: 'Your 1-minute timer is done!', level: 'info', durationMs: 30000 }]}
        onDismiss={onDismiss}
      />,
    );
    expect(screen.getByText('Your 1-minute timer is done!')).toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(6000); });
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => { vi.advanceTimersByTime(25000); });
    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it('never dismisses a persisting toast on its own', () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(<ToastContainer toasts={[{ id: 2, message: 'Saving…', level: 'info', persist: true }]} onDismiss={onDismiss} />);
    act(() => { vi.advanceTimersByTime(120000); });
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
