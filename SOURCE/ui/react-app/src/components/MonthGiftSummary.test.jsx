import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import MonthGiftSummary from './MonthGiftSummary';

const gift = { window_id: 'window', selected_level: 'max', monthly_price_cents: 2000, capacity_multiplier: 2,
  start_at: '2030-01-31T12:00:00Z', end_at: '2030-02-28T12:00:00Z', duration_months: 1 };
const projection = { base_subscription: { plan_id: 'pro_annual', status: 'canceled',
  funded_paid_through_at: '2030-01-31T12:00:00Z', cancel_at_period_end: true }, active_gift: gift,
  queued_gifts: [], return_after_gift: { plan_id: 'free', has_paid_access: false, base_canceled: true,
    cancellation_outcome: 'Canceled base access will not resume.' } };

describe('customer month-gift facts', () => {
  it('shows original canceled Pro, effective Max, calendar month and return to Free', () => {
    render(<MonthGiftSummary entitlement={projection} />);
    expect(screen.getByText(/Base subscription: Pro · canceled/)).toBeInTheDocument();
    expect(screen.getByText(/Current gift: Max · 2× Pro/)).toBeInTheDocument();
    expect(screen.getByText(/1 calendar month/)).toBeInTheDocument();
    expect(screen.getByText(/After complimentary access: Free/)).toBeInTheDocument();
    expect(screen.getByText(/does not restart the canceled subscription/)).toBeInTheDocument();
  });
  it('distinguishes selected flexible level and future queued gifts from current access', () => {
    render(<MonthGiftSummary entitlement={{ ...projection, active_gift: null,
      queued_gifts: [{ ...gift, selected_level: 'flexible', monthly_price_cents: 3000, capacity_multiplier: 3 }] }} />);
    expect(screen.getByText(/Scheduled gift: Flexible \$30.00 monthly level/)).toBeInTheDocument();
    expect(screen.queryByText(/Current gift:/)).not.toBeInTheDocument();
  });
});
