import { describe, it, expect } from 'vitest';
import { render, screen } from '../test/test-utils';
import DesktopRelayIndicator, { relayDeviceFromResult } from './DesktopRelayIndicator';

describe('relayDeviceFromResult', () => {
  it('returns the device name when the turn ran on the desktop', () => {
    const result = {
      ok: true,
      data: { message: 'done', executed_on: 'desktop', device_name: 'Viola Desktop (STUDIO)' },
    };
    expect(relayDeviceFromResult(result)).toBe('Viola Desktop (STUDIO)');
  });

  it('falls back to a generic name when executed_on is desktop but no name given', () => {
    const result = { data: { executed_on: 'desktop' } };
    expect(relayDeviceFromResult(result)).toBe('Your desktop');
  });

  it('returns null when the turn ran in the cloud', () => {
    expect(relayDeviceFromResult({ data: { message: 'cloud' } })).toBe(null);
    expect(relayDeviceFromResult({ data: { executed_on: 'cloud' } })).toBe(null);
  });

  it('handles a flat (non-enveloped) result and bad input', () => {
    expect(relayDeviceFromResult({ executed_on: 'desktop', device_name: 'Laptop' })).toBe('Laptop');
    expect(relayDeviceFromResult(null)).toBe(null);
    expect(relayDeviceFromResult('nonsense')).toBe(null);
  });
});

describe('DesktopRelayIndicator', () => {
  it('renders the "Connected to <device>" badge when a device name is given', () => {
    render(<DesktopRelayIndicator deviceName="Viola Desktop (STUDIO)" />);
    expect(screen.getByText(/Connected to Viola Desktop \(STUDIO\)/)).toBeInTheDocument();
  });

  it('renders nothing when no device name is given', () => {
    const { container } = render(<DesktopRelayIndicator deviceName={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
