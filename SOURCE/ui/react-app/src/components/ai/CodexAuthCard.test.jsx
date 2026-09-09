import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../test/test-utils';
import { apiFetch } from '../../hooks/useViolaApi';
import CodexAuthCard from './CodexAuthCard';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

describe('CodexAuthCard', () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('shows the checking state while auth status loads', () => {
    apiFetch.mockReturnValue(new Promise(() => {}));

    render(<CodexAuthCard />);

    expect(screen.getByText('Checking...')).toBeInTheDocument();
    expect(screen.getByText('Checking sign-in status...')).toBeInTheDocument();
  });

  it('launches sign-in and polls status until signed in', async () => {
    apiFetch
      .mockResolvedValueOnce({ signed_in: false, instructions: 'Run codex login.' })
      .mockResolvedValueOnce({ launched: true })
      .mockResolvedValueOnce({ signed_in: false })
      .mockResolvedValueOnce({
        signed_in: true,
        account_email: 'codex@example.com',
        expires_at: '2026-05-12T12:00:00Z',
      });

    render(<CodexAuthCard />);

    const signIn = await screen.findByRole('button', { name: /sign in with chatgpt/i });
    vi.useFakeTimers();
    fireEvent.click(signIn);
    await act(async () => {});
    expect(apiFetch).toHaveBeenCalledWith('/v1/codex/auth/launch', { method: 'POST' });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(screen.getByText('codex@example.com - expires 2026-05-12T12:00:00Z')).toBeInTheDocument();
  });

  it('signs out with confirmation payload', async () => {
    apiFetch
      .mockResolvedValueOnce({
        signed_in: true,
        account_email: 'codex@example.com',
      })
      .mockResolvedValueOnce({ deleted: true })
      .mockResolvedValueOnce({ signed_in: false });

    render(<CodexAuthCard />);

    fireEvent.click(await screen.findByRole('button', { name: /sign out/i }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/v1/codex/auth/signout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    }));
    expect(await screen.findByRole('button', { name: /sign in with chatgpt/i })).toBeInTheDocument();
  });
});
