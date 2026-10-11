import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { AuthProvider, useAuth } from './auth_context';
import { readBillingSubscription } from './billingEntitlement';

vi.mock('./billingEntitlement', () => ({ readBillingSubscription: vi.fn() }));
vi.mock('./gotrue_client', () => ({
  GOTRUE_STORAGE_KEY: 'synthetic-gotrue-session',
  externalOAuthAuthorizeUrl: vi.fn(),
  gotrueClient: {
    getSession: vi.fn(async () => ({ data: { session: {
      access_token: 'synthetic-access', refresh_token: 'synthetic-refresh',
      user: { id: 'account-a', email: 'account@example.com', app_metadata: {}, user_metadata: {} },
    } } })),
    onAuthStateChange: vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } })),
  },
}));

function Probe() {
  const auth = useAuth();
  return <span data-testid="plan">{auth.plan.planId}:{String(auth.hasPaidAccess)}</span>;
}

function billing(end, { queuedStart = null, gift = true } = {}) {
  return {
    status: gift ? 'active' : 'free', plan_id: gift ? 'max_monthly' : 'free',
    plan_family: gift ? 'max' : 'free', has_paid_access: gift, payment_provider: null,
    current_period_end: end, subscription_source: gift ? 'owner_month_gift' : 'commercial',
    entitlement: {
      user_id: 'account-a', base_subscription: { current_period_start: null },
      queued_gifts: queuedStart ? [{ start_at: queuedStart }] : [],
    },
  };
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2030-01-01T00:00:00Z')); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

it('re-reads a full calendar month deadline once, without rotating the existing session', async () => {
  const end = '2030-02-01T00:00:00Z';
  readBillingSubscription.mockResolvedValueOnce(billing(end)).mockResolvedValueOnce(billing(null, { gift: false }));
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });
  expect(screen.getByTestId('plan').textContent).toBe('max_monthly:true');
  await act(async () => { await vi.advanceTimersByTimeAsync(2147483647); });
  expect(readBillingSubscription).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('plan').textContent).toBe('max_monthly:true');
  await act(async () => { await vi.advanceTimersByTimeAsync(31 * 86400000 - 2147483647); });
  expect(readBillingSubscription).toHaveBeenCalledTimes(2);
  expect(readBillingSubscription.mock.calls[1][0]).toBe(readBillingSubscription.mock.calls[0][0]);
  expect(screen.getByTestId('plan').textContent).toBe('free:false');
});

it('re-reads at a queued gift start while the current entitlement has no expiry', async () => {
  const start = '2030-01-02T00:00:00Z';
  readBillingSubscription.mockResolvedValueOnce(billing(null, { gift: false, queuedStart: start }))
    .mockResolvedValueOnce(billing('2030-02-02T00:00:00Z'));
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });
  expect(screen.getByTestId('plan').textContent).toBe('free:false');
  await act(async () => { await vi.advanceTimersByTimeAsync(86400000); });
  expect(readBillingSubscription).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId('plan').textContent).toBe('max_monthly:true');
});
