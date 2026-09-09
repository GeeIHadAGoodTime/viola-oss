/**
 * React hooks for the EventBus.
 *
 * useEventBus   — subscribe to an event and get its latest payload as state.
 * useEventEmitter — get a stable callback to emit an event.
 *
 * Both hooks handle cleanup automatically on unmount.
 *
 * Usage:
 *   import { playerBus, PlayerEvents } from '../lib/eventBus';
 *   import { useEventBus, useEventEmitter } from './useEventBus';
 *
 *   function VolumeDisplay() {
 *     const volume = useEventBus(playerBus, PlayerEvents.VOLUME_CHANGED, 80);
 *     return <span>Volume: {volume}</span>;
 *   }
 *
 *   function VolumeControl() {
 *     const emitVolume = useEventEmitter(playerBus, PlayerEvents.VOLUME_CHANGED);
 *     return <input type="range" onChange={e => emitVolume(+e.target.value)} />;
 *   }
 */
import { useEffect, useState, useCallback } from 'react';

/**
 * Subscribe to an event bus event and return its latest payload.
 * Automatically unsubscribes when the component unmounts.
 *
 * @param {import('../lib/eventBus').EventBus} bus - Event bus instance
 * @param {string} event - Event name to subscribe to
 * @param {*} initialValue - Initial state value before any event fires
 * @returns {*} Latest event payload
 */
export function useEventBus(bus, event, initialValue) {
  const [value, setValue] = useState(initialValue);

  useEffect(() => {
    const unsub = bus.on(event, setValue);
    return unsub;
  }, [bus, event]);

  return value;
}

/**
 * Get a stable emitter function for an event.
 * The returned callback never changes reference (safe in dependency arrays).
 *
 * @param {import('../lib/eventBus').EventBus} bus - Event bus instance
 * @param {string} event - Event name to emit
 * @returns {function} Emit function: (data) => void
 */
export function useEventEmitter(bus, event) {
  return useCallback((data) => bus.emit(event, data), [bus, event]);
}
