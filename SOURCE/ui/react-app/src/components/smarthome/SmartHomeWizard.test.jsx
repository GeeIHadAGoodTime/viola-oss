/* eslint react/jsx-uses-vars: "error" */
import { beforeEach, describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '../../test/test-utils';
import SmartHomeWizard from './SmartHomeWizard';

function okResponse(data) {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve({ ok: true, data }),
    text: () => Promise.resolve(JSON.stringify({ ok: true, data })),
  };
}

function mockFetch({ settings, devices = [] }) {
  return vi.fn(async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const method = init.method || 'GET';

    if (url.endsWith('/v1/settings') && method === 'GET') {
      return okResponse({ settings });
    }

    if (url.endsWith('/v1/smarthome/discover') && method === 'POST') {
      return okResponse({ enabled: true, devices });
    }

    if (url.endsWith('/v1/settings') && method === 'PATCH') {
      const body = JSON.parse(init.body || '{}');
      return okResponse({ settings: { ...settings, ...(body.settings || {}) } });
    }

    return okResponse({});
  });
}

describe('SmartHomeWizard', () => {
  beforeEach(() => {
    // #4226: LAN discovery is desktop-only, so this wizard is a DesktopUpsell
    // on the cloud SPA. These cases are about the desktop scan flow.
    window.viola = {};
  });

  afterEach(() => {
    delete window.viola;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('renders the discovery-off empty state', async () => {
    vi.stubGlobal('fetch', mockFetch({
      settings: { network_discovery_enabled: false },
    }));

    render(<SmartHomeWizard />);

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: /Enable smart-home network discovery/i })).not.toBeChecked();
    });
    expect(screen.getByRole('button', { name: /Find devices on my network/i })).toBeDisabled();
    expect(screen.getByText(/Turn on local discovery/i)).toBeInTheDocument();
  });

  it('renders discovered devices after a scan', async () => {
    vi.stubGlobal('fetch', mockFetch({
      settings: { network_discovery_enabled: true },
      devices: [
        {
          display_name: 'Kitchen Hub',
          ip: '192.168.1.20',
          port: 8123,
          service_type: 'home_assistant',
          metadata: {},
        },
      ],
    }));

    const { user } = render(<SmartHomeWizard />);

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: /Enable smart-home network discovery/i })).toBeChecked();
    });
    await user.click(screen.getByRole('button', { name: /Find devices on my network/i }));

    expect(await screen.findByText('Kitchen Hub')).toBeInTheDocument();
    expect(screen.getByText('http://192.168.1.20:8123')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Set up smart-home hub/i })).toBeInTheDocument();
  });
});
