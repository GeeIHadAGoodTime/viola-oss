// Bindings enumerate different indices. Prefer the API's name+host-API token.
// Preserve a missing selection visibly, without silently changing saved settings.
export function outputDeviceOptions(devices, value = '') {
  const options = [{ value: '', label: 'System Default' }];
  for (const device of devices) {
    const token = String(device.selection ?? device.index);
    if (options.some(option => option.value === token)) continue;
    const ambiguous = devices.filter(other => String(other.selection ?? other.index) === token).length > 1;
    const name = device.hostapi ? `${device.name} (${device.hostapi})` : device.name;
    options.push({ value: token, label: ambiguous ? `${name} (ambiguous; using system default)` : name });
  }
  const selected = outputDeviceValue(value, devices);
  if (selected && !options.some(option => option.value === selected)) {
    let name = selected;
    if (selected.startsWith('portaudio:')) {
      try { name = JSON.parse(selected.slice('portaudio:'.length)).name || selected; }
      catch { /* Preserve malformed legacy values for explicit user correction. */ }
    }
    options.push({ value: selected, label: `${name} (unavailable or ambiguous; using system default)` });
  }
  return options;
}

export function outputDeviceValue(value, devices) {
  const stored = String(value ?? '').trim();
  if (['', '-1', 'default', 'system default', 'none'].includes(stored.toLowerCase())) return '';
  if (stored.startsWith('portaudio:')) {
    try {
      const identity = JSON.parse(stored.slice('portaudio:'.length));
      const device = devices.find(d => d.name === identity.name && d.hostapi === identity.hostapi);
      return device ? String(device.selection ?? device.index) : stored;
    } catch { return stored; }
  }
  const legacy = devices.find(d => String(d.index) === stored);
  if (legacy) return String(legacy.selection ?? legacy.index);
  const exact = devices.filter(d => d.name === stored);
  const named = exact.length ? exact : devices.filter(d => d.name.toLowerCase().includes(stored.toLowerCase()));
  return named.length === 1 ? String(named[0].selection ?? named[0].index) : stored;
}
