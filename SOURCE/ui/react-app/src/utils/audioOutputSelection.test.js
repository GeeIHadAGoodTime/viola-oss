import { describe, expect, it } from 'vitest';
import { outputDeviceOptions, outputDeviceValue } from './audioOutputSelection';

const token = (name, hostapi) => `portaudio:${JSON.stringify({ name, hostapi })}`;
const devices = [
  { index: 9, name: 'USB Speaker', hostapi: 'Host B', selection: token('USB Speaker', 'Host B') },
  { index: 3, name: 'USB Speaker', hostapi: 'Host A', selection: token('USB Speaker', 'Host A') },
  { index: 5, name: 'Built-in', hostapi: 'Host A', selection: token('Built-in', 'Host A') },
];

describe('output device selection', () => {
  it('offers stable binding-independent tokens and host labels', () => {
    expect(outputDeviceOptions(devices)[1]).toEqual({ value: devices[0].selection, label: 'USB Speaker (Host B)' });
  });
  it('retains explicit system default', () => {
    for (const value of [null, '', '  ', '-1', 'None']) expect(outputDeviceValue(value, devices)).toBe('');
  });
  it('displays legacy PyAudio index via its current endpoint without mutating it', () => {
    const stored = { output_device: '9' };
    expect(outputDeviceValue(stored.output_device, devices)).toBe(devices[0].selection);
    expect(stored.output_device).toBe('9');
  });
  it('supports older endpoint payloads without selection metadata', () => {
    expect(outputDeviceOptions([{ index: 2, name: 'Speaker' }])[1].value).toBe('2');
  });
  it('resolves unique legacy names and substrings', () => {
    expect(outputDeviceValue('Built-in', devices)).toBe(devices[2].selection);
    expect(outputDeviceValue('BUILT', devices)).toBe(devices[2].selection);
  });
  it('does not collapse names across host APIs', () => {
    expect(outputDeviceValue('USB Speaker', devices)).toBe('USB Speaker');
    expect(outputDeviceOptions(devices, 'USB Speaker').at(-1).label).toContain('ambiguous');
  });
  it('keeps disconnected tokens visible and unchanged', () => {
    const stored = token('Disconnected', 'Host A');
    expect(outputDeviceValue(stored, devices)).toBe(stored);
    expect(outputDeviceOptions(devices, stored).at(-1)).toEqual({ value: stored, label: 'Disconnected (unavailable or ambiguous; using system default)' });
  });
  it('handles malformed legacy tokens without throwing', () => {
    expect(outputDeviceOptions(devices, 'portaudio:bad').at(-1).value).toBe('portaudio:bad');
  });
  it('deduplicates ambiguous identities and labels fallback', () => {
    const options = outputDeviceOptions([devices[0], { ...devices[0], index: 10 }]);
    expect(options).toHaveLength(2);
    expect(options[1].label).toContain('ambiguous');
  });
  it('normalizes equivalent token JSON only for display', () => {
    const stored = 'portaudio:{ "hostapi": "Host B", "name": "USB Speaker" }';
    expect(outputDeviceValue(stored, devices)).toBe(devices[0].selection);
    expect(outputDeviceOptions(devices, stored)).toHaveLength(4);
  });
  it('keeps selected stable token meaningful after reorder', () => {
    const reordered = [...devices].reverse().map((device, index) => ({ ...device, index }));
    expect(outputDeviceValue(devices[0].selection, reordered)).toBe(devices[0].selection);
  });
});
