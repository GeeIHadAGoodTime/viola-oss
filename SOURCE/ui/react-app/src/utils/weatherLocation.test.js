import { describe, it, expect } from 'vitest';
import { autoDetectedWeatherLocation, shortPlaceName } from './weatherLocation';

describe('autoDetectedWeatherLocation', () => {
  it('prefers the fresh auto-detected location', () => {
    expect(autoDetectedWeatherLocation({ temperature: 61, auto_detected_location: 'Milwaukee, Wisconsin', location: 'Milwaukee' }))
      .toBe('Milwaukee, Wisconsin');
  });

  it('falls back to the location a cached answer carries', () => {
    expect(autoDetectedWeatherLocation({ temperature: 61, location: ' Milwaukee, Wisconsin ' })).toBe('Milwaukee, Wisconsin');
  });

  it('ignores the provider placeholder and missing data', () => {
    expect(autoDetectedWeatherLocation({ temperature: 61, location: 'Local' })).toBeNull();
    expect(autoDetectedWeatherLocation({ location: 'Milwaukee' })).toBeNull();
    expect(autoDetectedWeatherLocation(null)).toBeNull();
  });
});

describe('shortPlaceName', () => {
  it('keeps the city only', () => {
    expect(shortPlaceName('Milwaukee, Wisconsin')).toBe('Milwaukee');
    expect(shortPlaceName('Paris')).toBe('Paris');
    expect(shortPlaceName(null)).toBe('');
  });
});
