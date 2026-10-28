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

// 1.0.5 build 5: the complimentary-access text had no colour of its own and
// inherited the browser default (black) on the dark Account card.
function parseColor(value) {
  const rgba = /rgba?\(([^)]+)\)/.exec(value);
  if (rgba) {
    const [r, g, b, a = '1'] = rgba[1].split(',').map((part) => part.trim());
    return { r: Number(r), g: Number(g), b: Number(b), a: Number(a) };
  }
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (!hex) throw new Error(`unparsed colour ${value}`);
  const n = parseInt(hex[1], 16);
  return { r: n >> 16, g: (n >> 8) & 255, b: n & 255, a: 1 };
}
function luminance({ r, g, b }) {
  const channel = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}
function contrastOn(fg, bg) {
  const f = parseColor(fg); const b = parseColor(bg);
  const mixed = { r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a) };
  const [hi, lo] = [luminance(mixed), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe('complimentary-access text is readable', () => {
  it('sets its own text colours with AA contrast on the Account card', async () => {
    const { THEME } = await import('../config');
    render(<MonthGiftSummary entitlement={projection} />);
    const section = screen.getByTestId('month-gift-summary');
    expect(section.style.color).not.toBe('');
    expect(contrastOn(section.style.color, THEME.colors.bgElevated)).toBeGreaterThanOrEqual(4.5);
    const heading = screen.getByText('Complimentary access');
    expect(contrastOn(heading.style.color, THEME.colors.bgElevated)).toBeGreaterThanOrEqual(4.5);
  });
});
