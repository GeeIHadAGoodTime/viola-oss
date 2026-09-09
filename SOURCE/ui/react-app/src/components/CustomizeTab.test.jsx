import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '../test/test-utils';
import CustomizeTab from './CustomizeTab';
import { applyTheme } from '../config';

// CustomizeTab renders WakeWordSection which talks to the backend on mount;
// stub the API so the component mounts in isolation.
vi.mock('../hooks/useViolaApi', () => ({
  authFetch: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
  apiFetch: vi.fn(() => Promise.resolve({})),
}));

describe('CustomizeTab theme control (issue #769)', () => {
  beforeEach(() => {
    applyTheme('dark'); // baseline
  });
  afterEach(() => {
    applyTheme('dark');
    vi.restoreAllMocks();
  });

  it('applies the light theme to the DOM immediately when selected', () => {
    const updateLocal = vi.fn();
    render(<CustomizeTab localSettings={{ theme: 'dark' }} updateLocal={updateLocal} onReset={vi.fn()} />);

    // Baseline: dark background.
    expect(document.documentElement.style.getPropertyValue('--bg-void').toLowerCase()).toBe('#000000');

    // Open the Color Theme dropdown and choose Light.
    fireEvent.click(screen.getByRole('button', { name: /Color Theme:/i }));
    fireEvent.click(screen.getByRole('option', { name: /^Light$/i }));

    // The staged value is written AND the theme is applied to the DOM right away
    // (the pre-#769 control only did the former, so the theme never visibly changed).
    expect(updateLocal).toHaveBeenCalledWith('theme', 'light');
    expect(document.documentElement.style.getPropertyValue('--bg-void').toLowerCase()).toBe('#f5f5f7');
  });
});

// #3564: the TopBar's "Set your location" chip opens this tab, and SmartDisplay
// only fetches weather once weather_location is set. With no control here the
// chip was a dead end -- the browser app told the user to set a location it gave
// them no way to set, so weather could never be turned on at all. The cloud
// backend already resolves the signed-in caller's own saved location
// (ui/api/routes/weather.py:_resolve_cloud_user_location); only the input was
// missing.
describe('CustomizeTab weather location (#3564)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('offers a location field that stages weather_location', () => {
    const updateLocal = vi.fn();
    render(<CustomizeTab localSettings={{ theme: 'dark' }} updateLocal={updateLocal} />);

    const input = screen.getByLabelText('Location');
    expect(input).toHaveValue('');

    fireEvent.change(input, { target: { value: 'Milwaukee, WI' } });

    expect(updateLocal).toHaveBeenCalledWith('weather_location', 'Milwaukee, WI');
  });

  it('shows the location already saved on the account', () => {
    render(<CustomizeTab localSettings={{ weather_location: 'Chicago, IL' }} updateLocal={vi.fn()} />);

    expect(screen.getByLabelText('Location')).toHaveValue('Chicago, IL');
  });
});
