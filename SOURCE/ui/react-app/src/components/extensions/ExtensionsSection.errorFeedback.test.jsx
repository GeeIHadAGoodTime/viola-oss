/**
 * Regression coverage: extension errors must be surfaced, not swallowed.
 *
 * Two prior gaps:
 *  1. A failed MCP-server registration threw out of RegisterMCPForm.submit
 *     (no catch) — the modal stayed open with the spinner cleared, NO error
 *     message, and the rejection became an unhandled promise rejection. The
 *     parent status banner sits BEHIND the modal overlay, so setting status
 *     there would never be seen either. The reason must render inside the modal.
 *  2. The parent status banner coloured errors red only when the text included
 *     "failed", so real errors like "Could not load extensions." rendered in
 *     the neutral colour. Status now carries an explicit isError flag +
 *     role="alert".
 */
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '../../test/test-utils';
import { apiFetch } from '../../hooks/useViolaApi';
import ExtensionsSection from './ExtensionsSection';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

function mockInitialLoad() {
  apiFetch
    .mockResolvedValueOnce({ servers: [] })
    .mockResolvedValueOnce({ plugins: [] })
    .mockResolvedValueOnce({
      catalog: [{
        id: 'filesystem',
        name: 'Filesystem MCP',
        command: 'npx',
        args: ['-y', '@modelcontextprotocol/server-filesystem', '<path>'],
        description: 'Expose a local folder.',
        // No command_install_prompt → Install opens the manual register form.
      }],
    });
}

describe('ExtensionsSection — error feedback', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    // #4226: desktop surface — on cloud this section is a DesktopUpsell and
    // never fires the load these error paths are about.
    window.viola = {};
  });

  afterEach(() => {
    delete window.viola;
  });

  it('shows the failure reason inside the register modal and keeps it open', async () => {
    mockInitialLoad();
    render(<ExtensionsSection />);

    // Open the manual register form via the suggested entry.
    fireEvent.click(await screen.findByRole('button', { name: /install/i }));
    const heading = await screen.findByRole('heading', { name: /register mcp server/i });
    const modalForm = heading.closest('form');

    // Registration POST fails.
    const registerError = new Error('That command is not allowed.');
    apiFetch.mockRejectedValueOnce(registerError);

    fireEvent.click(within(modalForm).getByRole('button', { name: /^register$/i }));

    // The reason renders in the modal (an alert), and the modal stays open.
    await waitFor(() => {
      expect(within(modalForm).getByRole('alert')).toHaveTextContent(/that command is not allowed/i);
    });
    expect(screen.getByRole('heading', { name: /register mcp server/i })).toBeInTheDocument();
    // Spinner cleared — button is back to its idle label and enabled.
    expect(within(modalForm).getByRole('button', { name: /^register$/i })).not.toBeDisabled();
  });

  it('renders a load failure as an error alert (not neutral text)', async () => {
    // First of the three initial loads rejects.
    apiFetch.mockRejectedValueOnce(new Error('Could not load extensions.'));
    apiFetch.mockResolvedValue({ servers: [], plugins: [], catalog: [] });

    render(<ExtensionsSection />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/could not load extensions/i);
    });
  });
});
