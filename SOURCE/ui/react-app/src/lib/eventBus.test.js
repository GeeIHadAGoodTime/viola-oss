/**
 * Tests for the EventBus class.
 *
 * Covers: subscribe, emit, unsubscribe, multiple subscribers, error isolation,
 * once(), off(), listenerCount(), and the singleton exports.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EventBus, playerBus, PlayerEvents, VoiceEvents, UIEvents } from './eventBus';

describe('EventBus', () => {
  let bus;

  beforeEach(() => {
    bus = new EventBus();
  });

  describe('on() and emit()', () => {
    it('should deliver events to subscribers', () => {
      const handler = vi.fn();
      bus.on('test', handler);
      bus.emit('test', { value: 42 });

      expect(handler).toHaveBeenCalledOnce();
      expect(handler).toHaveBeenCalledWith({ value: 42 });
    });

    it('should support multiple subscribers for the same event', () => {
      const handler1 = vi.fn();
      const handler2 = vi.fn();
      bus.on('test', handler1);
      bus.on('test', handler2);
      bus.emit('test', 'hello');

      expect(handler1).toHaveBeenCalledWith('hello');
      expect(handler2).toHaveBeenCalledWith('hello');
    });

    it('should not call handlers for different events', () => {
      const handler = vi.fn();
      bus.on('alpha', handler);
      bus.emit('beta', 'data');

      expect(handler).not.toHaveBeenCalled();
    });

    it('should handle emit with no subscribers gracefully', () => {
      // Should not throw
      expect(() => bus.emit('nonexistent', 'data')).not.toThrow();
    });

    it('should pass undefined when emitting without data', () => {
      const handler = vi.fn();
      bus.on('test', handler);
      bus.emit('test');

      expect(handler).toHaveBeenCalledWith(undefined);
    });
  });

  describe('unsubscribe', () => {
    it('should return an unsubscribe function from on()', () => {
      const handler = vi.fn();
      const unsub = bus.on('test', handler);

      bus.emit('test', 1);
      expect(handler).toHaveBeenCalledOnce();

      unsub();
      bus.emit('test', 2);
      expect(handler).toHaveBeenCalledOnce(); // Still 1 — not called again
    });

    it('should only remove the specific handler when unsubscribing', () => {
      const handler1 = vi.fn();
      const handler2 = vi.fn();
      const unsub1 = bus.on('test', handler1);
      bus.on('test', handler2);

      unsub1();
      bus.emit('test', 'data');

      expect(handler1).not.toHaveBeenCalled();
      expect(handler2).toHaveBeenCalledWith('data');
    });
  });

  describe('once()', () => {
    it('should fire the handler only once', () => {
      const handler = vi.fn();
      bus.once('test', handler);

      bus.emit('test', 'first');
      bus.emit('test', 'second');

      expect(handler).toHaveBeenCalledOnce();
      expect(handler).toHaveBeenCalledWith('first');
    });

    it('should return an unsubscribe function that works before emit', () => {
      const handler = vi.fn();
      const unsub = bus.once('test', handler);

      unsub();
      bus.emit('test', 'data');

      expect(handler).not.toHaveBeenCalled();
    });
  });

  describe('error isolation', () => {
    it('should not let one handler error break others', () => {
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      const badHandler = vi.fn(() => { throw new Error('boom'); });
      const goodHandler = vi.fn();

      bus.on('test', badHandler);
      bus.on('test', goodHandler);
      bus.emit('test', 'data');

      expect(badHandler).toHaveBeenCalled();
      expect(goodHandler).toHaveBeenCalledWith('data');
      expect(consoleSpy).toHaveBeenCalledWith(
        expect.stringContaining('EventBus handler error'),
        expect.any(Error)
      );

      consoleSpy.mockRestore();
    });
  });

  describe('off()', () => {
    it('should remove all listeners for a specific event', () => {
      const handler1 = vi.fn();
      const handler2 = vi.fn();
      bus.on('test', handler1);
      bus.on('test', handler2);
      bus.on('other', vi.fn());

      bus.off('test');
      bus.emit('test', 'data');

      expect(handler1).not.toHaveBeenCalled();
      expect(handler2).not.toHaveBeenCalled();
    });

    it('should remove ALL listeners when called with no argument', () => {
      const h1 = vi.fn();
      const h2 = vi.fn();
      bus.on('alpha', h1);
      bus.on('beta', h2);

      bus.off();
      bus.emit('alpha', 'a');
      bus.emit('beta', 'b');

      expect(h1).not.toHaveBeenCalled();
      expect(h2).not.toHaveBeenCalled();
    });
  });

  describe('listenerCount()', () => {
    it('should return 0 for events with no listeners', () => {
      expect(bus.listenerCount('test')).toBe(0);
    });

    it('should return correct count for a specific event', () => {
      bus.on('test', () => {});
      bus.on('test', () => {});
      bus.on('other', () => {});

      expect(bus.listenerCount('test')).toBe(2);
      expect(bus.listenerCount('other')).toBe(1);
    });

    it('should return total count when called with no argument', () => {
      bus.on('a', () => {});
      bus.on('b', () => {});
      bus.on('b', () => {});

      expect(bus.listenerCount()).toBe(3);
    });

    it('should decrement after unsubscribe', () => {
      const unsub = bus.on('test', () => {});
      expect(bus.listenerCount('test')).toBe(1);

      unsub();
      expect(bus.listenerCount('test')).toBe(0);
    });
  });
});

describe('Exported singletons and constants', () => {
  it('playerBus should be an EventBus instance', () => {
    expect(playerBus).toBeInstanceOf(EventBus);
  });

  it('PlayerEvents should have expected keys', () => {
    expect(PlayerEvents.STATE_CHANGED).toBe('player:stateChanged');
    expect(PlayerEvents.TRACK_CHANGED).toBe('player:trackChanged');
    expect(PlayerEvents.POSITION_UPDATE).toBe('player:position');
    expect(PlayerEvents.VOLUME_CHANGED).toBe('player:volume');
    expect(PlayerEvents.QUEUE_UPDATED).toBe('player:queueUpdated');
  });

  it('VoiceEvents should have expected keys', () => {
    expect(VoiceEvents.LISTENING_STARTED).toBe('voice:listeningStarted');
    expect(VoiceEvents.TRANSCRIPT_RECEIVED).toBe('voice:transcriptReceived');
  });

  it('UIEvents should have expected keys', () => {
    expect(UIEvents.TOAST).toBe('ui:toast');
    expect(UIEvents.THEME_CHANGED).toBe('ui:themeChanged');
  });
});
