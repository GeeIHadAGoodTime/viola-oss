import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../../test/test-utils';
import { apiFetch } from '../../hooks/useViolaApi';
import ExtensionsSection from './ExtensionsSection';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

function mockInitialLoad() {
  apiFetch
    .mockResolvedValueOnce({
      servers: [{ name: 'github', status: 'connected (4 tools)', tool_count: 4, enabled: true }],
    })
    .mockResolvedValueOnce({
      plugins: [{ name: 'weather', description: 'Weather plugin', enabled: true, kind: 'builtin', tool_count: 2 }],
    })
    .mockResolvedValueOnce({
      catalog: [{
        id: 'filesystem',
        name: 'Filesystem MCP',
        command: 'npx',
        args: ['-y', '@modelcontextprotocol/server-filesystem', '<path>'],
        description: 'Expose a local folder.',
        command_install_prompt: 'Install filesystem mcp.',
      }],
    });
}

describe('ExtensionsSection', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    // #4226: the extension host is desktop-only, so this section is now a
    // DesktopUpsell on the cloud SPA. jsdom with no Qt bridge IS the cloud
    // surface, so declare the desktop one these cases are about.
    window.viola = {};
  });

  afterEach(() => {
    delete window.viola;
  });

  it('loads MCP servers, plugins, and suggested extensions', async () => {
    mockInitialLoad();

    render(<ExtensionsSection />);

    expect(await screen.findByText('github')).toBeInTheDocument();
    expect(screen.getByText('connected (4 tools)')).toBeInTheDocument();
    expect(screen.getByText('weather')).toBeInTheDocument();
    expect(screen.getByText('Filesystem MCP')).toBeInTheDocument();
  });

  it('falls back to the register form when conversational install is unavailable', async () => {
    mockInitialLoad();
    apiFetch.mockRejectedValueOnce(new Error('command unavailable'));

    render(<ExtensionsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /install/i }));

    await waitFor(() => expect(screen.getByRole('heading', { name: /register mcp server/i })).toBeInTheDocument());
    expect(screen.getByDisplayValue('filesystem')).toBeInTheDocument();
    expect(screen.getByDisplayValue('npx')).toBeInTheDocument();
  });
});
