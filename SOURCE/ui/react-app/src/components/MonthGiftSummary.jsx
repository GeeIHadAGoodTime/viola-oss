import React from 'react';
import PropTypes from 'prop-types';

const planName = (plan) => plan?.startsWith('max') ? 'Max' : plan?.startsWith('pro') ? 'Pro' : 'Free';
const date = (value) => value ? new Date(value).toLocaleString() : 'Unavailable';
const levelName = (gift) => gift.selected_level === 'flexible'
  ? `Flexible $${(gift.monthly_price_cents / 100).toFixed(2)} monthly level`
  : gift.selected_level === 'max' ? 'Max' : 'Pro';

export default function MonthGiftSummary({ entitlement }) {
  if (!entitlement || (!entitlement.active_gift && !entitlement.queued_gifts?.length)) return null;
  const base = entitlement.base_subscription;
  const gift = entitlement.active_gift;
  const returning = entitlement.return_after_gift;
  return <section aria-label="Complimentary month access">
    <h4>Complimentary access</h4>
    <p>Base subscription: {planName(base.plan_id)} · {base.status}.
      {base.funded_paid_through_at && <> Paid through {date(base.funded_paid_through_at)}.</>}</p>
    {base.current_period_start && Date.parse(base.current_period_start) > Date.now()
      && <p>Base paid access is scheduled to begin {date(base.current_period_start)}.</p>}
    {gift && <p>Current gift: {levelName(gift)} · {gift.capacity_multiplier}× Pro managed capacity,
      {' '}{gift.duration_months} {gift.duration_months === 1 ? 'calendar month' : 'calendar months'}.
      {' '}From {date(gift.start_at)} through {date(gift.end_at)}.</p>}
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
