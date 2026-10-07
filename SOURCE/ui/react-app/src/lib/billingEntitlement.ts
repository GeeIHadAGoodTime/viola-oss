import type { Session } from '@supabase/auth-js';
import { DEFAULTS } from '../config';

export type BillingSubscription = {
  status: string;
  plan_id: string;
  plan_family: string;
  has_paid_access: boolean;
  payment_provider: string | null;
  current_period_end: string | null;
  subscription_source: string | null;
};

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
    if (value.has_paid_access && (value.subscription_source || '').startsWith('admin_') && !until) {
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
    };
  } finally {
    clearTimeout(timer);
  }
}
