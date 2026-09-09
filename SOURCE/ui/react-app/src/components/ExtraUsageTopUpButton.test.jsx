import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ExtraUsageTopUpButton, { formatPrice } from './ExtraUsageTopUpButton';
import { apiFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

const AVAILABLE_OFFER = {
  available: true,
  price_cents: 1000,
  currency: 'usd',
  purchase_url: '/billing/extra-usage/checkout',
};

describe('ExtraUsageTopUpButton', () => {
  let openSpy;

  beforeEach(() => {
    apiFetch.mockReset();
    openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Leg 3: the affordance only exists when the top-up is really purchasable.
  it('renders nothing until the offer is known', () => {
    apiFetch.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = render(<ExtraUsageTopUpButton />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the offer reports the top-up unavailable', async () => {
    apiFetch.mockResolvedValueOnce({ ...AVAILABLE_OFFER, available: false });
    const { container } = render(<ExtraUsageTopUpButton />);
    // give the mount fetch a tick to resolve
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/billing/extra-usage'));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId('extra-usage-topup')).not.toBeInTheDocument();
  });

  it('renders nothing when the offer read fails', async () => {
    apiFetch.mockRejectedValueOnce(new Error('boom'));
    const { container } = render(<ExtraUsageTopUpButton />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/billing/extra-usage'));
    expect(container).toBeEmptyDOMElement();
  });

  // Leg 2: a real product caller POSTs the checkout path and opens the URL.
  it('buys the top-up: POSTs the offer path and opens the Stripe checkout URL', async () => {
    apiFetch
      .mockResolvedValueOnce(AVAILABLE_OFFER) // GET offer on mount
      .mockResolvedValueOnce({ checkout_url: 'https://checkout.stripe.com/c/pay/cs_test_123' }); // POST checkout

    render(<ExtraUsageTopUpButton />);

    const button = await screen.findByTestId('extra-usage-topup');
    expect(button).toHaveTextContent('Add extra usage ($10)');

    await userEvent.click(button);

    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/billing/extra-usage/checkout',
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    // The path it POSTs is exactly the one the offer handed back — no drift.
    const postCall = apiFetch.mock.calls.find((c) => c[1] && c[1].method === 'POST');
    expect(postCall[0]).toBe(AVAILABLE_OFFER.purchase_url);

    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        'https://checkout.stripe.com/c/pay/cs_test_123',
        '_blank',
        'noopener,noreferrer',
      ),
    );
  });

  it('does not open a tab when checkout returns no URL', async () => {
    apiFetch
      .mockResolvedValueOnce(AVAILABLE_OFFER)
      .mockResolvedValueOnce({ checkout_url: '' });

    render(<ExtraUsageTopUpButton />);
    const button = await screen.findByTestId('extra-usage-topup');
    await userEvent.click(button);

    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/billing/extra-usage/checkout',
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    expect(openSpy).not.toHaveBeenCalled();
  });
});

describe('formatPrice', () => {
  it('formats whole-dollar prices without cents', () => {
    expect(formatPrice(1000)).toBe('$10');
  });
  it('formats fractional prices with two decimals', () => {
    expect(formatPrice(1050)).toBe('$10.50');
  });
  it('returns empty for non-positive or non-numeric input', () => {
    expect(formatPrice(0)).toBe('');
    expect(formatPrice(-5)).toBe('');
    expect(formatPrice(NaN)).toBe('');
    expect(formatPrice('10')).toBe('');
  });
});
