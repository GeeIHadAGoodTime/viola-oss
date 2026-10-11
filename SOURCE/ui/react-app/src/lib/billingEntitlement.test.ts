import { afterEach, describe, expect, it, vi } from 'vitest';
import { readBillingSubscription } from './billingEntitlement';

const session = { access_token: 'existing-access', user: { id: 'user-a' } } as any;
const grant = {
  user_id: 'user-a', plan_id: 'pro_monthly', plan_family: 'pro', status: 'active',
  has_paid_access: true, payment_provider: null, subscription_source: 'admin_grant',
  current_period_end: new Date(Date.now() + 86400000).toISOString(),
};

afterEach(() => vi.unstubAllGlobals());

describe('canonical complimentary billing read', () => {
  it('uses current access token and canonical Pro despite empty GoTrue metadata', async () => {
    const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ ok: true, data: grant }) }));
    vi.stubGlobal('fetch', fetch);
    const value = await readBillingSubscription(session);
    expect(value.has_paid_access).toBe(true);
    expect(value.subscription_source).toBe('admin_grant');
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer existing-access');
    expect(fetch.mock.calls[0][0]).toContain('/billing/status');
  });
  it('rejects another account response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ...grant, user_id: 'user-b' }) })));
    await expect(readBillingSubscription(session)).rejects.toThrow('temporarily unavailable');
  });
  it('rejects a billing response without an authenticated account identity', async () => {
    const { user_id, ...unidentified } = grant;
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => unidentified })));
    await expect(readBillingSubscription(session)).rejects.toThrow('temporarily unavailable');
  });
  it('does not preserve paid access after the expiry', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({
      ...grant, current_period_end: new Date(Date.now()-1).toISOString(),
    }) })));
    expect((await readBillingSubscription(session)).has_paid_access).toBe(false);
  });
  it('keeps billing unavailable distinct from a Free plan', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503 })));
    await expect(readBillingSubscription(session)).rejects.toThrow('temporarily unavailable');
  });

  it('reads effective Max alongside canceled base Pro without granting from metadata', async () => {
    const start = new Date(Date.now() - 1000).toISOString();
    const end = new Date(Date.now() + 86400000).toISOString();
    const gift = { window_id: 'gift-a', user_id: 'user-a', selected_level: 'max', monthly_price_cents: 2000,
      capacity_multiplier: 2, start_at: start, end_at: end, calendar_anchor_at: start,
      duration_unit: 'month', duration_months: 1, state: 'confirmed' };
    const effective = { plan_id: 'max_monthly', plan_family: 'max', status: 'active', has_paid_access: true,
      current_period_start: start, current_period_end: end, subscription_source: 'owner_month_gift' };
    const entitlement = { user_id: 'user-a', effective,
      base_subscription: { plan_id: 'pro_annual', plan_family: 'pro', status: 'canceled', has_paid_access: false },
      active_gift: gift, queued_gifts: [] };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({
      ...grant, ...effective, entitlement,
    }) })));
    const result = await readBillingSubscription(session);
    expect(result.plan_id).toBe('max_monthly');
    expect(result.entitlement?.base_subscription.plan_id).toBe('pro_annual');
    expect(result.entitlement?.base_subscription.status).toBe('canceled');
  });
  it('rejects mismatched gift account and an annual effective period', async () => {
    const start = new Date(Date.now() - 1000).toISOString();
    const end = new Date(Date.now() + 86400000).toISOString();
    const gift = { window_id: 'gift-a', user_id: 'user-b', selected_level: 'max', monthly_price_cents: 2000,
      capacity_multiplier: 2, start_at: start, end_at: end, calendar_anchor_at: start,
      duration_unit: 'month', duration_months: 1, state: 'confirmed' };
    const effective = { plan_id: 'max_monthly', plan_family: 'max', status: 'active', has_paid_access: true,
      current_period_start: start, current_period_end: end, subscription_source: 'owner_month_gift' };
    const body = { ...grant, ...effective, entitlement: { user_id: 'user-a', effective,
      base_subscription: { plan_id: 'pro_annual', plan_family: 'pro', status: 'active', has_paid_access: true },
      active_gift: gift, queued_gifts: [] } };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => body })));
    await expect(readBillingSubscription(session)).rejects.toThrow('temporarily unavailable');
    gift.user_id = 'user-a';
    body.current_period_end = new Date(Date.now() + 365 * 86400000).toISOString();
    await expect(readBillingSubscription(session)).rejects.toThrow('temporarily unavailable');
  });
});
