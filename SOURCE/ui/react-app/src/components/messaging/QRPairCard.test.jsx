import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../test/test-utils';
import { apiFetch } from '../../hooks/useViolaApi';
import QRPairCard, { buildQrModules } from './QRPairCard';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

async function flushPromises() {
  await act(async () => {});
}

describe('QRPairCard', () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('renders the checking state while the first status request is pending', () => {
    apiFetch.mockReturnValue(new Promise(() => {}));

    render(<QRPairCard channel="telegram" />);

    expect(screen.getByRole('status', { name: '' })).toHaveTextContent('Checking Telegram connection...');
    expect(screen.queryByRole('button', { name: /pair with telegram/i })).toBeNull();
  });

  it('renders the unlinked state after a disconnected status response', async () => {
    apiFetch.mockResolvedValueOnce({ linked: false });

    render(<QRPairCard channel="telegram" />);

    expect(await screen.findByRole('button', { name: /pair with telegram/i })).toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledWith('/auth/telegram/status');
  });

  it('renders the linked state with account details from the status payload', async () => {
    apiFetch.mockResolvedValueOnce({
      linked: true,
      telegram_username: 'jay',
      telegram_first_name: 'Jay',
      linked_at: '2026-05-01T12:00:00Z',
    });

    render(<QRPairCard channel="telegram" />);

    expect(await screen.findByText('@jay')).toBeInTheDocument();
    expect(screen.getByText('Connected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /unpair/i })).toBeInTheDocument();
  });

  it('generates an in-app QR image and polls until Telegram links', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-12T12:00:00Z'));

    const deepLink = 'https://t.me/violavoice_bot?start=abc123';
    apiFetch
      .mockResolvedValueOnce({ linked: false })
      .mockResolvedValueOnce({
        token: 'abc123',
        deep_link: deepLink,
        bot_username: 'violavoice_bot',
        expires_in_minutes: 15,
      })
      .mockResolvedValueOnce({ linked: false })
      .mockResolvedValueOnce({
        linked: true,
        telegram_username: 'jay',
        linked_at: '2026-05-12T12:01:00Z',
      });

    render(<QRPairCard channel="telegram" />);
    await flushPromises();

    fireEvent.click(screen.getByRole('button', { name: /pair with telegram/i }));
    await flushPromises();

    const qr = screen.getByRole('img', { name: 'Scan to pair Telegram with Viola' });
    expect(qr.tagName.toLowerCase()).toBe('svg');
    expect(buildQrModules(deepLink)).toEqual(expect.any(Array));
    expect(document.querySelector('img[src*="api.qrserver.com"]')).toBeNull();
    expect(screen.getByRole('link', { name: deepLink })).toHaveAttribute('href', deepLink);
    expect(screen.getByText('Link expires in 15:00')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByText('Link expires in 14:57')).toBeInTheDocument();
    expect(screen.queryByText('@jay')).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByText('@jay')).toBeInTheDocument();
  });

  it('expires a stale token and returns to the unlinked state', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-12T12:00:00Z'));

    apiFetch
      .mockResolvedValueOnce({ linked: false })
      .mockResolvedValueOnce({
        token: 'abc123',
        deep_link: 'https://t.me/violavoice_bot?start=abc123',
        expires_in_minutes: 0.01,
      });

    render(<QRPairCard channel="telegram" />);
    await flushPromises();

    fireEvent.click(screen.getByRole('button', { name: /pair with telegram/i }));
    await flushPromises();

    expect(screen.getByRole('img', { name: 'Scan to pair Telegram with Viola' })).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Telegram link expired');
    expect(screen.getByRole('button', { name: /pair with telegram/i })).toBeInTheDocument();
  });

  it('cleans up pairing polling after unmount', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-12T12:00:00Z'));

    apiFetch
      .mockResolvedValueOnce({ linked: false })
      .mockResolvedValueOnce({
        token: 'abc123',
        deep_link: 'https://t.me/violavoice_bot?start=abc123',
        expires_in_minutes: 15,
      });

    const { unmount } = render(<QRPairCard channel="telegram" />);
    await flushPromises();

    fireEvent.click(screen.getByRole('button', { name: /pair with telegram/i }));
    await flushPromises();

    expect(screen.getByRole('img', { name: 'Scan to pair Telegram with Viola' })).toBeInTheDocument();
    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(apiFetch).toHaveBeenCalledTimes(2);
  });

  it('confirms unlink before calling the Telegram unlink endpoint', async () => {
    apiFetch
      .mockResolvedValueOnce({
        linked: true,
        telegram_username: 'jay',
        linked_at: '2026-05-12T12:00:00Z',
      })
      .mockResolvedValueOnce({ ok: true, message: 'Telegram account disconnected' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<QRPairCard channel="telegram" />);

    fireEvent.click(await screen.findByRole('button', { name: /unpair/i }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/auth/telegram/unlink', { method: 'POST' }));
    expect(window.confirm).toHaveBeenCalledWith('Disconnect Telegram? Your conversation history will be deleted.');
    expect(await screen.findByRole('button', { name: /pair with telegram/i })).toBeInTheDocument();
  });

  it('fails silently into the unconnected state when the first-open probe fails (#773)', async () => {
    // Opening the Connections tab fires the status probe with zero user action.
    // A failed auto-probe (e.g. not signed in yet, or a transient hiccup) must
    // NOT greet the user with a red "Unable to check ..." banner — it presents
    // as the unconnected state with the connect affordance instead.
    apiFetch.mockRejectedValueOnce(new Error('offline'));

    render(<QRPairCard channel="telegram" />);

    // Connect affordance is shown...
    expect(await screen.findByRole('button', { name: /pair with telegram/i })).toBeInTheDocument();
    // ...and no error banner appears on first open.
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
