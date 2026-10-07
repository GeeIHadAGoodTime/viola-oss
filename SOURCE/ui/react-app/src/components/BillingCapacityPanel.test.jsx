import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import BillingCapacityPanel, { capacityInputCents } from './BillingCapacityPanel';
import { apiFetch } from '../hooks/useViolaApi';
import { billingPaymentUrl } from '../hooks/useBillingCapacity';

vi.mock('../hooks/useViolaApi', () => ({ apiFetch: vi.fn() }));
const BASE = '/v1/billing/capacity';
const amounts = [1200, ...Array.from({ length: 99 }, (_, index) => (index + 2) * 1000)];
const catalog = { available: true, catalog_version: 'capacity_v1', allowed_monthly_price_cents: amounts,
  selections: amounts.map((amount) => ({ monthly_price_cents: amount, annual_price_cents: amount * 10,
    capacity_multiplier: amount === 1200 ? 1 : amount / 1000 })) };
const account = { available: true, monthly_price_cents: 2000, recurring_amount_cents: 2000,
  interval: 'month', capacity_multiplier: 2, usage_percent: 49, usage_resets_at: '2026-11-07T00:00:00Z',
  renews_at: '2026-11-07T00:00:00Z', actions: { increase: true, lower: true }, terms_version: '3.4', privacy_version: '3.6' };
const quote = { quote_id: 'owned-quote', action: 'increase', target_monthly_price_cents: 4000, interval: 'month',
  currency: 'usd', amount_due_now_cents: 1000, next_recurring_amount_cents: 4000, capacity_multiplier: 4,
  additional_current_cycle_multiplier: 1, tax_included_in_quote: true, terms_version: '3.4', privacy_version: '3.6',
  effective_at: '2026-10-07T00:00:00Z', renews_at: '2026-11-07T00:00:00Z', expires_at: '2099-10-07T00:00:00Z' };
let handler;

function requests(path) { return apiFetch.mock.calls.filter(([url]) => url === `${BASE}${path}`); }
async function selectForty() {
  fireEvent.click(await screen.findByRole('button', { name: 'Add more usage' }));
  fireEvent.change(screen.getByLabelText('Monthly dollars'), { target: { value: '40' } });
}
async function previewForty() {
  await selectForty();
  fireEvent.click(screen.getByRole('button', { name: 'Preview change' }));
  await screen.findByLabelText('Change quote');
}

describe('in-app capacity billing', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.spyOn(window, 'open').mockImplementation(() => null);
    handler = async (path) => {
      if (path === `${BASE}/catalog`) return catalog;
      if (path === BASE) return account;
      if (path === `${BASE}/quote`) return quote;
      if (path === `${BASE}/change`) return { id: 'operation-1', status: 'applied' };
      throw new Error(`Unexpected request: ${path}`);
    };
    apiFetch.mockReset().mockImplementation((...args) => handler(...args));
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it('shows paid state and allows adding usage before exhaustion without charging on selection', async () => {
    render(<BillingCapacityPanel accountId="account-1" />);
    await selectForty();
    expect(screen.getByText('49% used')).toBeInTheDocument();
    expect(screen.getByText('4× Pro')).toBeInTheDocument();
    expect(requests('/quote')).toHaveLength(0);
    expect(requests('/change')).toHaveLength(0);
    expect(screen.getByRole('slider')).toHaveAttribute('step', '1');
    fireEvent.change(screen.getByRole('slider'), { target: { value: '0' } });
    expect(screen.getByLabelText('Monthly dollars')).toHaveValue('12');
    fireEvent.change(screen.getByRole('slider'), { target: { value: '1' } });
    expect(screen.getByLabelText('Monthly dollars')).toHaveValue('20');
  });

  it('rejects fractional, exponent and $22 input without rounding or quote requests', async () => {
    render(<BillingCapacityPanel accountId="account-1" />);
    await selectForty();
    for (const value of ['22', '30.5', '-40', '1e2', '1001']) {
      fireEvent.change(screen.getByLabelText('Monthly dollars'), { target: { value } });
      expect(screen.getByRole('button', { name: 'Preview change' })).toBeDisabled();
    }
    expect(requests('/quote')).toHaveLength(0);
    expect(capacityInputCents('20')).toBe(2000);
    expect(capacityInputCents('20.1')).toBeNull();
  });

  it('uses the real quote and explicit consent, then refreshes entitlement without replaying work', async () => {
    const refresh = vi.fn().mockResolvedValue({ success: true });
    const event = vi.fn();
    window.addEventListener('viola:billing-entitlement-changed', event);
    render(<BillingCapacityPanel accountId="account-1" refreshUser={refresh} />);
    await previewForty();
    expect(JSON.parse(requests('/quote')[0][1].body)).toEqual({ monthly_price_cents: 4000, interval: 'month', return_at_renewal: false });
    const confirm = screen.getByRole('button', { name: 'Confirm upgrade — $10.00 today' });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.click(confirm);
    await screen.findByText(/Your capacity is updated/);
    const sent = JSON.parse(requests('/change')[0][1].body);
    expect(sent).toMatchObject({ quote_id: 'owned-quote', terms_accepted: true, terms_version: '3.4', privacy_version: '3.6' });
    expect(sent.idempotency_key).toBeTruthy();
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(event).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('viola:billing-capacity:account-1')).toBeNull();
    window.removeEventListener('viola:billing-entitlement-changed', event);
  });

  it('discards an in-flight quote when a customer changes the selection', async () => {
    let resolve;
    const original = handler;
    handler = (path) => path === `${BASE}/quote` ? new Promise((done) => { resolve = done; }) : original(path);
    render(<BillingCapacityPanel accountId="account-1" />);
    await selectForty();
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }));
    fireEvent.change(screen.getByLabelText('Monthly dollars'), { target: { value: '50' } });
    await act(async () => { resolve(quote); });
    expect(screen.queryByLabelText('Change quote')).not.toBeInTheDocument();
    expect(requests('/change')).toHaveLength(0);
  });

  it('clears affirmative consent whenever the reviewed selection changes', async () => {
    render(<BillingCapacityPanel accountId="account-1" />);
    await previewForty();
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.change(screen.getByLabelText('Monthly dollars'), { target: { value: '50' } });
    expect(screen.queryByLabelText('Change quote')).not.toBeInTheDocument();
    expect(requests('/change')).toHaveLength(0);
  });

  it('prominently shows the full annual payment', async () => {
    render(<BillingCapacityPanel accountId="account-1" />);
    await selectForty();
    fireEvent.click(screen.getByRole('button', { name: 'Annual' }));
    expect(screen.getByText('$400.00/year')).toBeInTheDocument();
    expect(screen.getByText(/\$40.00\/month capacity level/)).toBeInTheDocument();
    expect(requests('/change')).toHaveLength(0);
  });

  it('recovers an uncertain confirmation with the original operation key', async () => {
    const original = handler;
    let confirmations = 0;
    handler = async (path, options) => {
      if (path === `${BASE}/change` && ++confirmations === 1) throw new Error('Response lost');
      return original(path, options);
    };
    render(<BillingCapacityPanel accountId="account-1" />);
    await previewForty();
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.click(screen.getByRole('button', { name: /Confirm upgrade/ }));
    await screen.findByText(/result could not be confirmed/);
    const first = JSON.parse(requests('/change')[0][1].body);
    expect(screen.getByRole('button', { name: 'Add more usage' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Recover change status' }));
    await screen.findByText(/Your capacity is updated/);
    expect(JSON.parse(requests('/change')[1][1].body)).toEqual(first);
  });

  it('restores a pending payment after restart and coalesces browser return events', async () => {
    localStorage.setItem('viola:billing-capacity:account-1', JSON.stringify({ operation_id: 'operation-pending',
      idempotency_key: 'preserved-key', request: { quote_id: 'old-quote' } }));
    let finish;
    const original = handler;
    handler = (path) => path === `${BASE}/changes/operation-pending`
      ? new Promise((done) => { finish = done; }) : original(path);
    const refresh = vi.fn();
    render(<BillingCapacityPanel accountId="account-1" refreshUser={refresh} />);
    await waitFor(() => expect(finish).toBeDefined());
    fireEvent(window, new Event('focus'));
    fireEvent(document, new Event('visibilitychange'));
    expect(requests('/changes/operation-pending')).toHaveLength(1);
    expect(requests('/change')).toHaveLength(0);
    await act(async () => finish({ id: 'operation-pending', status: 'applied' }));
    await screen.findByText(/Your capacity is updated/);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('does not expose another account’s saved payment attempt', async () => {
    localStorage.setItem('viola:billing-capacity:account-other', JSON.stringify({ operation_id: 'private-operation',
      idempotency_key: 'other-key', request: { quote_id: 'private-quote' } }));
    render(<BillingCapacityPanel accountId="account-1" />);
    await selectForty();
    expect(screen.queryByRole('button', { name: 'Recover change status' })).not.toBeInTheDocument();
    expect(requests('/changes/private-operation')).toHaveLength(0);
  });

  it('keeps scheduled lowering visible and requires consent to undo it', async () => {
    const original = handler;
    handler = (path) => path === BASE ? { ...account, actions: { lower: true, cancel_scheduled: true },
      scheduled_change: { monthly_price_cents: 1200, interval: 'month', effective_at: account.renews_at } } : original(path);
    render(<BillingCapacityPanel accountId="account-1" />);
    const undo = await screen.findByRole('button', { name: 'Undo scheduled change' });
    expect(undo).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Lower my monthly capacity' })).toBeInTheDocument();
  });

  it('uses first-purchase quote/change and recovers payment authentication in place', async () => {
    const original = handler;
    handler = (path, options) => {
      if (path === BASE) return { ...account, monthly_price_cents: 0, recurring_amount_cents: 0,
        actions: { checkout: true }, interval: 'month', capacity_multiplier: 0 };
      if (path === `${BASE}/quote`) return { ...quote, action: 'checkout', target_monthly_price_cents: 2000,
        amount_due_now_cents: 2000, capacity_multiplier: 2, additional_current_cycle_multiplier: 2 };
      if (path === `${BASE}/change` || path === `${BASE}/changes/operation-auth`) {
        return { id: 'operation-auth', status: 'requires_action', checkout_url: 'https://checkout.stripe.com/c/pay/owned' };
      }
      return original(path, options);
    };
    render(<BillingCapacityPanel accountId="account-1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add more usage' }));
    expect(screen.getByLabelText('Monthly dollars')).toHaveValue('20');
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }));
    await screen.findByLabelText('Change quote');
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.click(screen.getByRole('button', { name: /Continue to payment — \$20.00 today/ }));
    expect(await screen.findByRole('link', { name: 'Continue payment' })).toHaveAttribute('href', 'https://checkout.stripe.com/c/pay/owned');
    expect(window.open).toHaveBeenCalledWith('https://checkout.stripe.com/c/pay/owned', '_blank', 'noopener,noreferrer');
    expect(screen.getByRole('button', { name: 'Add more usage' })).toBeDisabled();
    expect(requests('/change')).toHaveLength(1);
  });

  it('requires a fresh confirmation after an expired quote or revision conflict', async () => {
    const original = handler;
    handler = (path, options) => path === `${BASE}/change`
      ? Promise.reject(Object.assign(new Error('Expired quote'), { status: 409 })) : original(path, options);
    render(<BillingCapacityPanel accountId="account-1" />);
    await previewForty();
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.click(screen.getByRole('button', { name: /Confirm upgrade/ }));
    await screen.findByText(/Review a fresh quote/);
    expect(screen.queryByLabelText('Change quote')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Recover change status' })).not.toBeInTheDocument();
    expect(localStorage.getItem('viola:billing-capacity:account-1')).toBeNull();
  });

  it('retains paid capacity when payment fails and never refreshes it as funded', async () => {
    const original = handler;
    const refresh = vi.fn();
    handler = (path, options) => path === `${BASE}/change`
      ? { id: 'operation-declined', status: 'failed' } : original(path, options);
    render(<BillingCapacityPanel accountId="account-1" refreshUser={refresh} />);
    await previewForty();
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.click(screen.getByRole('button', { name: /Confirm upgrade/ }));
    await screen.findByText(/change was failed/);
    expect(screen.getByText('$20.00/month · 2× Pro')).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    expect(window.open).not.toHaveBeenCalled();
  });

  it('cannot confirm a quote missing legal or monetary authority', async () => {
    const original = handler;
    handler = (path) => path === `${BASE}/quote` ? { ...quote, amount_due_now_cents: undefined } : original(path);
    render(<BillingCapacityPanel accountId="account-1" />);
    await selectForty();
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }));
    await screen.findByText(/quote is incomplete/);
    expect(screen.queryByRole('button', { name: /Confirm upgrade/ })).not.toBeInTheDocument();
    expect(requests('/change')).toHaveLength(0);
  });

  it('retains an intervention state without reporting paid capacity applied or starting another purchase', async () => {
    const original = handler;
    const refresh = vi.fn();
    handler = (path, options) => path === `${BASE}/change` || path === `${BASE}/changes/operation-review`
      ? { id: 'operation-review', status: 'remediation_required' } : original(path, options);
    render(<BillingCapacityPanel accountId="account-1" refreshUser={refresh} />);
    await previewForty();
    fireEvent.click(screen.getByRole('checkbox', { name: /I agree to/ }));
    fireEvent.click(screen.getByRole('button', { name: /Confirm upgrade/ }));
    await screen.findByText(/payment needs billing review/);
    expect(screen.queryByText(/Your capacity is updated/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add more usage' })).toBeDisabled();
    expect(refresh).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem('viola:billing-capacity:account-1')).operation_id).toBe('operation-review');
  });

  it('shows refunded or disputed effective capacity and follows server action availability', async () => {
    const original = handler;
    handler = (path) => path === BASE ? { ...account, billing_review_required: true, effective_capacity_multiplier: 1,
      actions: { increase: false, lower: false } } : original(path);
    render(<BillingCapacityPanel accountId="account-1" />);
    await screen.findByText(/capacity is under billing review/);
    expect(screen.getByRole('button', { name: 'Add more usage' })).toBeDisabled();
    expect(screen.getByText('Available managed capacity')).toBeInTheDocument();
    expect(screen.getByText('1× Pro')).toBeInTheDocument();
    expect(requests('/quote')).toHaveLength(0);
  });
});

describe('payment handoff URL', () => {
  it('accepts Stripe payment authentication and rejects credentials, non-HTTPS and lookalikes', () => {
    expect(billingPaymentUrl('https://invoice.stripe.com/i/test')).toBe('https://invoice.stripe.com/i/test');
    for (const value of ['javascript:alert(1)', 'http://invoice.stripe.com/i/test',
      'https://invoice.stripe.com.evil.test/i/test', 'https://user@invoice.stripe.com/i/test']) {
      expect(billingPaymentUrl(value)).toBe('');
    }
  });
});
