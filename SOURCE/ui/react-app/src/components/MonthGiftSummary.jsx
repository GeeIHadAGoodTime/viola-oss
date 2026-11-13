import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';

const planName = (plan) => plan?.startsWith('max') ? 'Max' : plan?.startsWith('pro') ? 'Pro' : 'Free';
const date = (value) => value ? new Date(value).toLocaleString() : 'Unavailable';
const levelName = (gift) => gift.selected_level === 'flexible'
  ? `Flexible $${(gift.monthly_price_cents / 100).toFixed(2)} monthly level`
  : gift.selected_level === 'max' ? 'Max' : 'Pro';

// "Free · free." said the plan twice: a status only counts when it adds something.
const baseSummary = (base) => {
  const plan = planName(base.plan_id);
  const status = String(base.status || '').replaceAll('_', ' ');
  return status && status.toLowerCase() !== plan.toLowerCase() ? `${plan} · ${status}` : plan;
};

// One string per line of text. A number written as its own JSX node
// (`{gift.capacity_multiplier}× Pro`) becomes a separate text node, and the
// laptop's accessibility read of the Account page (1.0.5 build 10) showed
// "Pro · x Pro managed capacity" and "% used" with the numbers missing, while
// every line built as one string kept its number.
const giftLine = (gift) => {
  const multiplier = Number.isFinite(gift.capacity_multiplier) ? `${gift.capacity_multiplier}× Pro managed capacity` : 'Pro managed capacity';
  const months = `${gift.duration_months} ${gift.duration_months === 1 ? 'calendar month' : 'calendar months'}`;
  return `Current gift: ${levelName(gift)} · ${multiplier}, ${months}. From ${date(gift.start_at)} through ${date(gift.end_at)}.`;
};

export default function MonthGiftSummary({ entitlement }) {
  if (!entitlement || (!entitlement.active_gift && !entitlement.queued_gifts?.length)) return null;
  const base = entitlement.base_subscription;
  const gift = entitlement.active_gift;
  const returning = entitlement.return_after_gift;
  // Explicit theme colours: with none set, this text inherited the browser's
  // default black and was unreadable on the dark Account card (1.0.5 build 5).
  const colors = THEME.colors;
  return <section aria-label="Complimentary month access" data-testid="month-gift-summary"
    style={{ color: colors.textSecondary, fontSize: '14px', lineHeight: 1.5 }}>
    <h4 style={{ color: colors.textPrimary, margin: '0 0 8px' }}>Complimentary access</h4>
    <p>{`Base subscription: ${baseSummary(base)}.`}
      {base.funded_paid_through_at && <> Paid through {date(base.funded_paid_through_at)}.</>}</p>
    {base.current_period_start && Date.parse(base.current_period_start) > Date.now()
      && <p>Base paid access is scheduled to begin {date(base.current_period_start)}.</p>}
    {gift && <p>{giftLine(gift)}</p>}
    {(entitlement.queued_gifts || []).map((queued) => <p key={queued.window_id}>
      Scheduled gift: {levelName(queued)}, {date(queued.start_at)} through {date(queued.end_at)}.
    </p>)}
    {returning && <p>After complimentary access: {returning.has_paid_access ? planName(returning.plan_id) : 'Free'}
      {returning.resumes_at && <> from {date(returning.resumes_at)}</>}.
      {returning.cancellation_outcome && <> {returning.cancellation_outcome}</>}</p>}
    {(base.cancel_at_period_end || base.status === 'canceled') && <p>Your base subscription cancellation remains in place.
      {' '}Gift access ends on its listed date and does not restart the canceled subscription.</p>}
    {base.provider_deferred_renews_at && <p>Next base subscription renewal: {date(base.provider_deferred_renews_at)}.</p>}
    {!base.provider_deferred_renews_at && base.provider_renews_at && <p>Base subscription renewal: {date(base.provider_renews_at)}.</p>}
    <p>The gift has no automatic renewal. Existing usage and purchased allowance remain accounted for.</p>
  </section>;
}

MonthGiftSummary.propTypes = { entitlement: PropTypes.object };
