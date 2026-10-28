import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../test/test-utils';
import ContentCard from './ContentCard';

describe('ContentCard gate review cards', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders payment gate URLs as links and opens the hosted confirmation URL', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const confirmationUrl = 'https://api.useviola.com/confirm/token-123';

    const { user } = render(
      <ContentCard
        data={{
          type: 'gate_review',
          gate_kind: 'payment',
          title: 'Payment review required',
          body: `Review this payment at ${confirmationUrl}`,
          subject_url: confirmationUrl,
          origin_channel: 'telegram',
          cta: { label: 'Review and pay', action: 'open_url', url: confirmationUrl },
          secondary_cta: { label: "Don't pay", action: 'send_chat', text: "no don't pay" },
          tertiary_cta: { label: 'Confirm selected card', action: 'send_chat', text: 'yes confirm payment' },
        }}
        onDismiss={vi.fn()}
      />
    );

    const links = screen.getAllByRole('link', { name: /api\.useviola\.com\/confirm/i });
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute('href', confirmationUrl);
    expect(links[0]).toHaveAttribute('target', '_blank');
    expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer');

    await user.click(screen.getByRole('button', { name: 'Review and pay' }));
    expect(openSpy).toHaveBeenCalledWith(confirmationUrl, '_blank', 'noopener,noreferrer');
    expect(screen.queryByRole('button', { name: /show me the page/i })).not.toBeInTheDocument();

    const fetchSpy = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchSpy);
    await user.click(screen.getByRole('button', { name: 'Confirm selected card' }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const [, request] = fetchSpy.mock.calls[0];
    expect(request.headers).toMatchObject({
      'X-API-Key': 'test-api-key',
      'Content-Type': 'application/json',
    });
    expect(JSON.parse(request.body)).toMatchObject({
      text: 'yes confirm payment',
      channel: 'telegram',
    });
  });

  it('routes send_chat actions through the originating channel', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = render(
      <ContentCard
        data={{
          type: 'gate_review',
          gate_kind: 'signature',
          title: 'Legal signature required',
          body: 'Please review before signing.',
          origin_channel: 'discord',
          cta: { label: 'Sign and continue', action: 'send_chat', text: 'yes sign it and continue' },
          secondary_cta: { label: "Don't sign", action: 'send_chat', text: "no don't sign" },
        }}
        onDismiss={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Sign and continue' }));

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const [, request] = fetchSpy.mock.calls[0];
    expect(request.headers).toMatchObject({
      'X-API-Key': 'test-api-key',
      'Content-Type': 'application/json',
    });
    expect(JSON.parse(request.body)).toMatchObject({
      text: 'yes sign it and continue',
      channel: 'discord',
    });
  });

  it('renders sign-in / create-account CTAs for the account_required card (Path A)', async () => {
    const { user } = render(
      <ContentCard
        data={{
          type: 'account_required',
          title: 'Account required',
          body: 'Sign in or create a Viola account to use managed AI.',
          cta: { label: 'Sign in', url: 'https://useviola.com/login' },
          secondary_cta: { label: 'Create account', url: 'https://useviola.com/login?tab=register' },
        }}
        onDismiss={vi.fn()}
      />
    );

    // Sign in button is rendered and clickable
    const signInBtn = await screen.findByRole('button', { name: 'Sign in' });
    expect(signInBtn).toBeTruthy();
    const createBtn = screen.getByRole('button', { name: 'Create account' });
    expect(createBtn).toBeTruthy();

    // Clicking Sign in opens useviola.com/login (no pair-code mint, no polling).
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    await user.click(signInBtn);
    expect(openSpy).toHaveBeenCalledWith(
      'https://useviola.com/login',
      '_blank',
      'noopener,noreferrer',
    );
    openSpy.mockRestore();
  }, 7000);
});

// 1.0.5 build 5: after a typed command, the reply card's full-screen backdrop
// swallowed the next tap, so the top bar's "..." menu ignored the first click.
describe('ContentCard does not swallow taps outside the card', () => {
  it('dismisses on an outside tap and lets that tap reach the control under it', async () => {
    vi.useFakeTimers();
    try {
      const onDismiss = vi.fn();
      const onMenu = vi.fn();
      render(
        <>
          <button type="button" onClick={onMenu}>Open menu</button>
          <ContentCard data={{ type: 'detail', title: 'Answer', body: 'Long answer' }} onDismiss={onDismiss} />
        </>,
      );
      expect(screen.getByTestId('content-card-backdrop').style.pointerEvents).toBe('none');
      expect(screen.getByRole('dialog').style.pointerEvents).toBe('auto');

      const menu = screen.getByRole('button', { name: 'Open menu' });
      fireEvent.pointerDown(menu);
      fireEvent.click(menu);

      expect(onMenu).toHaveBeenCalledTimes(1);
      act(() => { vi.advanceTimersByTime(1000); });
      expect(onDismiss).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a tap inside the card does not count as outside', () => {
    const onDismiss = vi.fn();
    render(<ContentCard data={{ type: 'account_required', title: 'Account required', body: 'Sign in' }} onDismiss={onDismiss} />);
    fireEvent.pointerDown(screen.getByRole('dialog'));
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
