/**
 * Tests for the Toast notification system.
 *
 * Covers: useToast hook (add, remove, max limit), ToastContainer rendering,
 * and auto-dismiss behavior.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, renderHook } from '../../test/test-utils';
import React from 'react';
import ToastContainer, { useToast } from '../../components/Toast';

describe('useToast', () => {
  it('should start with no toasts', () => {
    const { result } = renderHook(() => useToast());
    expect(result.current.toasts).toEqual([]);
  });

  it('should add a toast', () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ message: 'Hello', level: 'info' });
    });

    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0].message).toBe('Hello');
    expect(result.current.toasts[0].level).toBe('info');
  });

  it('should assign unique IDs to toasts', () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ message: 'First', level: 'info' });
      result.current.addToast({ message: 'Second', level: 'error' });
    });

    const ids = result.current.toasts.map(t => t.id);
    expect(new Set(ids).size).toBe(2);
  });

  it('should remove a toast by ID', () => {
    const { result } = renderHook(() => useToast());

    let id;
    act(() => {
      id = result.current.addToast({ message: 'Remove me', level: 'warning' });
    });

    expect(result.current.toasts).toHaveLength(1);

    act(() => {
      result.current.removeToast(id);
    });

    expect(result.current.toasts).toHaveLength(0);
  });

  it('should limit to MAX_TOASTS (5), dropping oldest', () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      for (let i = 0; i < 7; i++) {
        result.current.addToast({ message: `Toast ${i}`, level: 'info' });
      }
    });

    expect(result.current.toasts).toHaveLength(5);
    // Oldest should have been dropped
    const messages = result.current.toasts.map(t => t.message);
    expect(messages).toContain('Toast 6');
    expect(messages).not.toContain('Toast 0');
  });

  it('should not add a toast with empty message', () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ message: '', level: 'info' });
    });

    expect(result.current.toasts).toHaveLength(0);
  });

  it('should default level to info', () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ message: 'Default level' });
    });

    expect(result.current.toasts[0].level).toBe('info');
  });
});

describe('ToastContainer', () => {
  it('should render nothing when toasts array is empty', () => {
    const { container } = render(
      <ToastContainer toasts={[]} onDismiss={vi.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('should render toast messages', () => {
    const toasts = [
      { id: 1, message: 'Error occurred', level: 'error' },
      { id: 2, message: 'Just info', level: 'info' },
    ];

    render(<ToastContainer toasts={toasts} onDismiss={vi.fn()} />);

    expect(screen.getByText('Error occurred')).toBeInTheDocument();
    expect(screen.getByText('Just info')).toBeInTheDocument();
  });

  it('should have accessible dismiss buttons', () => {
    const toasts = [
      { id: 1, message: 'Dismiss me', level: 'warning' },
    ];

    render(<ToastContainer toasts={toasts} onDismiss={vi.fn()} />);

    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument();
  });

  it('should call onDismiss when dismiss button is clicked', async () => {
    const onDismiss = vi.fn();
    const toasts = [
      { id: 42, message: 'Click dismiss', level: 'info' },
    ];

    const { user } = render(
      <ToastContainer toasts={toasts} onDismiss={onDismiss} />
    );

    await user.click(screen.getByRole('button', { name: /dismiss/i }));

    // The ToastItem calls onDismiss after animation delay (300ms)
    // Since we have fake timers from setup, let's just verify it's eventually called.
    // Note: ToastItem has a setTimeout before calling onDismiss.
    // We need to advance timers to trigger it.
    await vi.waitFor(() => {
      expect(onDismiss).toHaveBeenCalledWith(42);
    }, { timeout: 1000 });
  });

  it('should use aria-live polite for accessibility', () => {
    const toasts = [
      { id: 1, message: 'Accessible', level: 'info' },
    ];

    const { container } = render(
      <ToastContainer toasts={toasts} onDismiss={vi.fn()} />
    );

    const liveRegion = container.querySelector('[aria-live="polite"]');
    expect(liveRegion).toBeTruthy();
  });
});
