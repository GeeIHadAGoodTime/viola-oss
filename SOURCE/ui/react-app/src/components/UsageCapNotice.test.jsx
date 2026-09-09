import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import UsageCapNotice, { formatResetHint } from './UsageCapNotice';
import { apiFetch } from '../hooks/useViolaApi';

// UsageCapNotice now embeds ExtraUsageTopUpButton, which reads the offer on
// mount. Mock the API so the embedded button is deterministic (unavailable by
// default, so these existing assertions about the upgrade route are unchanged).
vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

const DENIAL = { plan: 'free', period: 'weekly', resetsAt: '2026-08-01T00:00:00+00:00' };

describe('UsageCapNotice', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ available: false, price_cents: 1000, currency: 'usd', purchase_url: '/billing/extra-usage/checkout' });
  });

  // The Terms-primary option: when the top-up is purchasable it renders inside
  // the cap notice alongside the upgrade route.
  it('surfaces the extra-usage top-up when it is available', async () => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ available: true, price_cents: 1000, currency: 'usd', purchase_url: '/billing/extra-usage/checkout' });
    render(<UsageCapNotice capDenial={DENIAL} onUpgrade={() => {}} />);
    expect(await screen.findByTestId('extra-usage-topup')).toBeInTheDocument();
    // The plan-upgrade route is still present.
    expect(screen.getByTestId('usage-cap-upgrade')).toBeInTheDocument();
  });

  it('shows only the upgrade route when the top-up is unavailable', async () => {
    render(<UsageCapNotice capDenial={DENIAL} onUpgrade={() => {}} />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/billing/extra-usage'));
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
    expect(button).toHaveTextContent('Upgrade your plan');
    await userEvent.click(button);
    expect(onUpgrade).toHaveBeenCalledTimes(1);
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
