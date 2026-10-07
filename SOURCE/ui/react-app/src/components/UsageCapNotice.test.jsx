import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import UsageCapNotice, { formatResetHint } from './UsageCapNotice';
import { apiFetch } from '../hooks/useViolaApi';

// The exhaustion notice must open Billing, never initiate a charge itself.
vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

const DENIAL = { plan: 'free', period: 'weekly', resetsAt: '2026-08-01T00:00:00+00:00' };

describe('UsageCapNotice', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ available: false, price_cents: 1000, currency: 'usd', purchase_url: '/billing/extra-usage/checkout' });
  });

  it('opens billing for capacity instead of starting a legacy booster purchase', async () => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ available: true, price_cents: 1000, currency: 'usd', purchase_url: '/billing/extra-usage/checkout' });
    render(<UsageCapNotice capDenial={DENIAL} onUpgrade={() => {}} />);
    expect(screen.queryByTestId('extra-usage-topup')).not.toBeInTheDocument();
    expect(screen.getByTestId('usage-cap-upgrade')).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it('shows only the upgrade route when the top-up is unavailable', async () => {
    render(<UsageCapNotice capDenial={DENIAL} onUpgrade={() => {}} />);
    expect(apiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('extra-usage-topup')).not.toBeInTheDocument();
    expect(screen.getByTestId('usage-cap-upgrade')).toBeInTheDocument();
  });
  it('renders nothing when the turn was not capped', () => {
    const { container } = render(<UsageCapNotice capDenial={null} onUpgrade={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  // The whole point of C-077: the denial must offer something to tap.
  it('offers a tappable upgrade action on a capped turn', async () => {
    const onUpgrade = vi.fn();
    render(<UsageCapNotice capDenial={DENIAL} onUpgrade={onUpgrade} />);
    const button = screen.getByTestId('usage-cap-upgrade');
    expect(button).toHaveTextContent('Add more usage');
    await userEvent.click(button);
    expect(onUpgrade).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/local models or your own provider key/)).toBeInTheDocument();
    expect(screen.getByText(/Your work is preserved/)).toBeInTheDocument();
  });

  it('shows when the allowance resets', () => {
    render(<UsageCapNotice capDenial={DENIAL} onUpgrade={() => {}} />);
    expect(screen.getByText(/Your allowance resets/)).toBeInTheDocument();
  });

  // The dispatch fallback path denies with no cap detail at all. The button is
  // the part that matters, so it must still render.
  it('still offers the action when the cap carries no plan or reset detail', () => {
    render(<UsageCapNotice capDenial={{ plan: '', period: '', resetsAt: '' }} onUpgrade={() => {}} />);
    expect(screen.getByTestId('usage-cap-upgrade')).toBeInTheDocument();
    expect(screen.queryByText(/Your allowance resets/)).not.toBeInTheDocument();
  });

  it('drops an unparseable reset instead of rendering "Invalid Date"', () => {
    expect(formatResetHint('not-a-date')).toBe('');
    expect(formatResetHint('')).toBe('');
    expect(formatResetHint(null)).toBe('');
    render(<UsageCapNotice capDenial={{ ...DENIAL, resetsAt: 'not-a-date' }} onUpgrade={() => {}} />);
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Your allowance resets/)).not.toBeInTheDocument();
  });
});
