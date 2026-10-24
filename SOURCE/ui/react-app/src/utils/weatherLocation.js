// The place a no-city weather answer is for. When the user has not saved a
// weather location, the backend resolves one itself (IP geolocation on the
// desktop) and a fresh lookup names it in `auto_detected_location`; a cached
// answer only carries `location`. "Local" is the provider's placeholder for
// "no idea", so it does not count. Returns null when the answer names no place.
export function autoDetectedWeatherLocation(data) {
  if (!data || data.temperature === undefined) return null;
  for (const value of [data.auto_detected_location, data.location]) {
    const text = typeof value === 'string' ? value.trim() : '';
    if (text && text.toLowerCase() !== 'local') return text;
  }
  return null;
}

// Short place name for the top bar: "Milwaukee, Wisconsin" -> "Milwaukee".
export function shortPlaceName(location) {
  if (typeof location !== 'string') return '';
  return location.split(',')[0].trim();
}
