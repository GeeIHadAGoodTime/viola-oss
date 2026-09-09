/**
 * Tests for useEventBus and useEventEmitter hooks.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { EventBus } from '../../lib/eventBus';
import { useEventBus, useEventEmitter } from '../../hooks/useEventBus';

describe('useEventBus', () => {
  let bus;

  beforeEach(() => {
    bus = new EventBus();
  });

  it('should return the initial value before any event', () => {
    const { result } = renderHook(() => useEventBus(bus, 'test', 42));
    expect(result.current).toBe(42);
  });

  it('should update when an event is emitted', () => {
    const { result } = renderHook(() => useEventBus(bus, 'test', 0));

    act(() => {
      bus.emit('test', 99);
    });

    expect(result.current).toBe(99);
  });

  it('should handle object payloads', () => {
    const { result } = renderHook(() => useEventBus(bus, 'state', null));

    act(() => {
      bus.emit('state', { volume: 50, is_playing: true });
    });

    expect(result.current).toEqual({ volume: 50, is_playing: true });
  });

  it('should unsubscribe on unmount', () => {
    const { unmount } = renderHook(() => useEventBus(bus, 'test', 0));

    expect(bus.listenerCount('test')).toBe(1);
    unmount();
    expect(bus.listenerCount('test')).toBe(0);
  });

  it('should not react to events for other names', () => {
    const { result } = renderHook(() => useEventBus(bus, 'alpha', 'init'));

    act(() => {
      bus.emit('beta', 'wrong');
    });

    expect(result.current).toBe('init');
  });
});

describe('useEventEmitter', () => {
  let bus;

  beforeEach(() => {
    bus = new EventBus();
  });

  it('should return a function that emits events', () => {
    const handler = vi.fn();
    bus.on('test', handler);

    const { result } = renderHook(() => useEventEmitter(bus, 'test'));

    act(() => {
      result.current('payload');
    });

    expect(handler).toHaveBeenCalledWith('payload');
  });

  it('should return a stable function reference', () => {
    const { result, rerender } = renderHook(() => useEventEmitter(bus, 'test'));
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
