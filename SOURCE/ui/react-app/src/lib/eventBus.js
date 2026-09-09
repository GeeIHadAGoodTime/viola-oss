/**
 * Lightweight pub/sub event bus for decoupling component communication.
 *
 * Use for player state, voice events, and other cross-component updates
 * that currently flow through callback refs in usePlayerState.
 *
 * This does NOT replace WebSocket — it is for component-to-component
 * communication within the React app.
 *
 * Future usage (after SmartDisplay decomposition):
 *
 *   // In a player controls component:
 *   import { playerBus, PlayerEvents } from '../lib/eventBus';
 *   import { useEventBus } from '../hooks/useEventBus';
 *
 *   function PlayerControls() {
 *     const volume = useEventBus(playerBus, PlayerEvents.VOLUME_CHANGED, 80);
 *     return <VolumeSlider value={volume} />;
 *   }
 *
 *   // In the WebSocket handler:
 *   playerBus.emit(PlayerEvents.VOLUME_CHANGED, msg.payload.volume);
 */
class EventBus {
  #listeners = new Map();

  /**
   * Subscribe to an event.
   * @param {string} event - Event name
   * @param {function} handler - Event handler
   * @returns {function} Unsubscribe function
   */
  on(event, handler) {
    if (!this.#listeners.has(event)) {
      this.#listeners.set(event, new Set());
    }
    this.#listeners.get(event).add(handler);
    return () => this.#listeners.get(event)?.delete(handler);
  }

  /**
   * Subscribe to an event for a single emission only.
   * @param {string} event - Event name
   * @param {function} handler - Event handler
   * @returns {function} Unsubscribe function
   */
  once(event, handler) {
    const wrapper = (data) => {
      unsub();
      handler(data);
    };
    const unsub = this.on(event, wrapper);
    return unsub;
  }

  /**
   * Emit an event to all subscribers.
   * @param {string} event - Event name
   * @param {*} data - Event payload
   */
  emit(event, data) {
    this.#listeners.get(event)?.forEach((handler) => {
      try {
        handler(data);
      } catch (e) {
        console.error(`EventBus handler error for "${event}":`, e);
      }
    });
  }

  /**
   * Remove all listeners for an event, or all listeners if no event specified.
   * @param {string} [event] - Optional event name
   */
  off(event) {
    if (event) {
      this.#listeners.delete(event);
    } else {
      this.#listeners.clear();
    }
  }

  /**
   * Get the number of listeners for an event, or total listeners if no event.
   * @param {string} [event] - Optional event name
   * @returns {number} Listener count
   */
  listenerCount(event) {
    if (event) {
      return this.#listeners.get(event)?.size || 0;
    }
    let total = 0;
    this.#listeners.forEach((set) => { total += set.size; });
    return total;
  }
}

// Singleton for player state communication
export const playerBus = new EventBus();

// Event type constants — use these instead of raw strings to avoid typos
export const PlayerEvents = {
  STATE_CHANGED: 'player:stateChanged',
  TRACK_CHANGED: 'player:trackChanged',
  POSITION_UPDATE: 'player:position',
  VOLUME_CHANGED: 'player:volume',
  QUEUE_UPDATED: 'player:queueUpdated',
};

// Voice event constants
export const VoiceEvents = {
  LISTENING_STARTED: 'voice:listeningStarted',
  LISTENING_STOPPED: 'voice:listeningStopped',
  TRANSCRIPT_RECEIVED: 'voice:transcriptReceived',
  RESPONSE_RECEIVED: 'voice:responseReceived',
};

// General UI event constants
export const UIEvents = {
  TOAST: 'ui:toast',
  THEME_CHANGED: 'ui:themeChanged',
  MODAL_OPENED: 'ui:modalOpened',
  MODAL_CLOSED: 'ui:modalClosed',
};

// Export the class for creating additional bus instances in tests or isolated modules
export { EventBus };

export default playerBus;
