/**
 * Format a Date or timestamp respecting the user's time_display_format setting.
 * @param {Date|string|number} dateOrTimestamp
 * @param {'auto'|'12h'|'24h'} format - from userSettings.time_display_format
 * @returns {string}
 */
export function formatTimeDisplay(dateOrTimestamp, format = 'auto') {
  const date = dateOrTimestamp instanceof Date ? dateOrTimestamp : new Date(dateOrTimestamp);
  const options = { hour: 'numeric', minute: '2-digit' };
  if (format === '12h') options.hour12 = true;
  else if (format === '24h') options.hour12 = false;
  // 'auto' uses browser locale default (no hour12 specified)
  return date.toLocaleTimeString([], options);
}
