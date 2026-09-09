/**
 * Desktop notification utilities using the Web Notifications API.
 *
 * Usage:
 *   import { showNotification } from './utils/notifications';
 *   showNotification('Now Playing', { body: 'Song - Artist' }, enabled);
 */

// Cooldown tracking per notification tag to prevent spam during rapid track changes
const _lastNotificationTime = {};
const NOTIFICATION_COOLDOWN_MS = 5000;
const NOTIFICATION_ICON_URL = `${import.meta.env.BASE_URL || '/'}icons/icon-192.svg`;

/**
 * Request notification permission from the browser.
 * Call this when the user enables notifications.
 * @returns {Promise<string>} 'granted', 'denied', or 'default'
 */
export async function requestNotificationPermission() {
  if (!('Notification' in window)) return 'denied';
  if (Notification.permission === 'granted') return 'granted';
  if (Notification.permission === 'denied') return 'denied';
  return await Notification.requestPermission();
}

/**
 * Show a desktop notification if permitted and enabled.
 * Rate-limited per tag: suppresses if last notification with the same tag
 * was less than 5 seconds ago (prevents spam during rapid track skipping).
 * @param {string} title
 * @param {object} options - { body, icon, tag, ... }
 * @param {boolean} enabled - the show_notifications setting value
 */
export function showNotification(title, options = {}, enabled = true) {
  if (!enabled) return;
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;

  // Rate-limit by tag (default tag '_default' for untagged notifications)
  const tag = options.tag || '_default';
  const now = Date.now();
  const lastTime = _lastNotificationTime[tag] || 0;
  if (now - lastTime < NOTIFICATION_COOLDOWN_MS) return;
  _lastNotificationTime[tag] = now;

  try {
    new Notification(title, {
      icon: NOTIFICATION_ICON_URL,
      ...options,
    });
  } catch (e) {
    // Fallback: some environments don't support the Notification constructor
    console.warn('Notification failed:', e);
  }
}
