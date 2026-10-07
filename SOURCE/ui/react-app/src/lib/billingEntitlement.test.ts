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
});
