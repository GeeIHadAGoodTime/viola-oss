import { describe, expect, it } from 'vitest';

import {
  CONDITION_UNAVAILABLE_TEXT,
  UNKNOWN_CONDITION,
  describeCondition,
  isKnownConditionText,
  normalizeConditionKey,
} from './weatherCondition';

describe('normalizeConditionKey', () => {
  it('reads the backend condition_code vocabulary', () => {
    expect(normalizeConditionKey('partly_cloudy')).toBe('partly-cloudy');
    expect(normalizeConditionKey('thunderstorm')).toBe('storm');
    expect(normalizeConditionKey('overcast')).toBe('overcast');
    expect(normalizeConditionKey('hail')).toBe('hail');
    expect(normalizeConditionKey('smoke')).toBe('smoke');
  });

  it('reads raw provider text when there is no code', () => {
    expect(normalizeConditionKey(null, 'Mostly Cloudy')).toBe('cloudy');
    expect(normalizeConditionKey(null, 'Chance Showers And Thunderstorms')).toBe('storm');
    expect(normalizeConditionKey(null, 'Patchy Fog')).toBe('fog');
    expect(normalizeConditionKey(null, 'Mostly Sunny')).toBe('clear');
  });

  it('prefers the first value that identifies a sky state', () => {
    expect(normalizeConditionKey('unknown', 'Light Rain')).toBe('rain');
    expect(normalizeConditionKey(undefined, undefined, 'Clear')).toBe('clear');
  });

  it('returns unknown rather than guessing', () => {
    // Every one of these used to resolve to partly-cloudy.
    expect(normalizeConditionKey(undefined)).toBe(UNKNOWN_CONDITION);
    expect(normalizeConditionKey('')).toBe(UNKNOWN_CONDITION);
    expect(normalizeConditionKey('unknown')).toBe(UNKNOWN_CONDITION);
    expect(normalizeConditionKey('Unknown')).toBe(UNKNOWN_CONDITION);
    expect(normalizeConditionKey(null, 'Weather')).toBe(UNKNOWN_CONDITION);
    expect(normalizeConditionKey('funnel cloud stuff we never mapped')).not.toBe(UNKNOWN_CONDITION);
    expect(normalizeConditionKey('a condition nobody has ever heard of')).toBe(UNKNOWN_CONDITION);
  });
});

describe('isKnownConditionText', () => {
  it('rejects placeholders a payload may still carry', () => {
    expect(isKnownConditionText('Unknown')).toBe(false);
    expect(isKnownConditionText('  ')).toBe(false);
    expect(isKnownConditionText('N/A')).toBe(false);
    expect(isKnownConditionText('Weather')).toBe(false);
    expect(isKnownConditionText('Light rain')).toBe(true);
  });
});

describe('describeCondition', () => {
  it('shows the provider text when there is one', () => {
    expect(describeCondition('Light rain', 'rain')).toBe('Light rain');
  });

  it('humanises the code when only the code is real', () => {
    expect(describeCondition('Unknown', 'partly_cloudy')).toBe('Partly cloudy');
  });

  it('says the condition is unavailable when nothing is known', () => {
    expect(describeCondition(undefined, 'unknown')).toBe(CONDITION_UNAVAILABLE_TEXT);
    expect(describeCondition('Unknown', undefined)).toBe(CONDITION_UNAVAILABLE_TEXT);
    expect(describeCondition(null, null, { short: true })).toBe('Unavailable');
  });
});
