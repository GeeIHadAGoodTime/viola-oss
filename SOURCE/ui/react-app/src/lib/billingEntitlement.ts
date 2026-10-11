import type { Session } from '@supabase/auth-js';
import { DEFAULTS } from '../config';

export type GiftWindow = {
  window_id: string; user_id: string; selected_level: 'pro' | 'max' | 'flexible';
  monthly_price_cents: number; capacity_multiplier: number; start_at: string; end_at: string;
  calendar_anchor_at: string; duration_unit: 'month'; duration_months: number; state: 'confirmed';
};
export type SubscriptionFacts = {
  plan_id: string; plan_family: string; status: string; has_paid_access: boolean;
  current_period_start?: string | null; current_period_end?: string | null;
  payment_provider?: string | null; subscription_source?: string;
  monthly_price_cents?: number | null; capacity_multiplier?: number | null;
  canceled_at?: string | null; cancel_at_period_end?: boolean;
  funded_paid_through_at?: string | null; usage_anchor_at?: string | null;
  provider_renews_at?: string | null; provider_deferred_renews_at?: string | null;
};
export type EntitlementProjection = {
  user_id: string; effective: SubscriptionFacts; base_subscription: SubscriptionFacts;
  active_gift: GiftWindow | null; queued_gifts: GiftWindow[];
  return_after_gift?: { plan_id: string; has_paid_access: boolean; base_canceled: boolean;
    gift_survives_base_cancellation: boolean; cancellation_outcome?: string | null; resumes_at?: string | null } | null;
};

export type BillingSubscription = {
  status: string;
  plan_id: string;
  plan_family: string;
  has_paid_access: boolean;
  payment_provider: string | null;
  current_period_end: string | null;
  subscription_source: string | null;
  entitlement?: EntitlementProjection;
};

function validGift(gift: GiftWindow, accountId: string): boolean {
  return Boolean(gift && gift.user_id === accountId && typeof gift.window_id === 'string' && gift.window_id
    && ['pro', 'max', 'flexible'].includes(gift.selected_level) && gift.duration_unit === 'month'
    && gift.state === 'confirmed' && Number.isSafeInteger(gift.duration_months) && gift.duration_months > 0
    && Number.isSafeInteger(gift.monthly_price_cents) && gift.monthly_price_cents > 0
    && Number.isFinite(gift.capacity_multiplier) && gift.capacity_multiplier > 0
    && Number.isFinite(Date.parse(gift.start_at)) && Number.isFinite(Date.parse(gift.end_at))
    && Number.isFinite(Date.parse(gift.calendar_anchor_at)) && Date.parse(gift.start_at) < Date.parse(gift.end_at));
}

function readProjection(value: any, accountId: string): EntitlementProjection | undefined {
  if (!value.entitlement) return undefined;
  const projection = value.entitlement as EntitlementProjection;
  if (projection.user_id !== accountId || !projection.effective || !projection.base_subscription
      || !Array.isArray(projection.queued_gifts)
      || typeof projection.base_subscription.plan_id !== 'string'
      || typeof projection.base_subscription.has_paid_access !== 'boolean'
      || projection.effective.plan_id !== value.plan_id
      || projection.effective.plan_family !== value.plan_family
      || projection.effective.status !== value.status
      || projection.effective.subscription_source !== value.subscription_source
      || projection.effective.has_paid_access !== value.has_paid_access
      || projection.queued_gifts.some((gift) => !validGift(gift, accountId))
      || (projection.active_gift && !validGift(projection.active_gift, accountId))) {
    throw new Error('Plan information is temporarily unavailable.');
  }
  if (value.subscription_source === 'owner_month_gift') {
    const gift = projection.active_gift;
    if (!gift || Date.parse(value.current_period_end) !== Date.parse(gift.end_at)
        || Date.parse(projection.effective.current_period_start || '') !== Date.parse(gift.start_at)
        || Date.parse(gift.start_at) > Date.now()) {
      throw new Error('Plan information is temporarily unavailable.');
    }
  }
  return projection;
}

/** Read billing truth with the existing session; never redeem a refresh token. */
export async function readBillingSubscription(session: Session): Promise<BillingSubscription> {
  const origin = (window as Window & { __VIOLA_BASE_URL__?: string }).__VIOLA_BASE_URL__ || window.location.origin;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULTS.BILLING_READ_TIMEOUT_MS);
  try {
    const response = await fetch(`${origin}/billing/status`, {
      headers: { Authorization: `Bearer ${session.access_token}` },
      credentials: 'include',
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error('Plan information is temporarily unavailable.');
    const envelope = await response.json();
    const value = envelope?.data ?? envelope;
    if (envelope?.ok === false || !value || typeof value.plan_id !== 'string'
        || typeof value.has_paid_access !== 'boolean' || typeof value.status !== 'string'
        || value.user_id !== session.user.id) {
      throw new Error('Plan information is temporarily unavailable.');
    }
    const until = typeof value.current_period_end === 'string' ? value.current_period_end : null;
    if (until && !Number.isFinite(Date.parse(until))) {
      throw new Error('Plan information is temporarily unavailable.');
    }
    if (value.has_paid_access && ((value.subscription_source || '').startsWith('admin_')
        || value.subscription_source === 'owner_month_gift') && !until) {
      throw new Error('Plan information is temporarily unavailable.');
    }
    const projection = readProjection(value, session.user.id);
    if (value.subscription_source === 'owner_month_gift' && !projection) {
      throw new Error('Plan information is temporarily unavailable.');
    }
    return {
      status: value.status,
      plan_id: value.plan_id,
      plan_family: value.plan_family || value.plan_id.split('_')[0],
      has_paid_access: value.has_paid_access && (!until || Date.parse(until) > Date.now()),
      payment_provider: value.payment_provider || null,
      current_period_end: until,
      subscription_source: value.subscription_source || null,
      ...(projection ? { entitlement: projection } : {}),
    };
  } finally {
    clearTimeout(timer);
  }
}
