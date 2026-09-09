/**
 * ExtraUsageTopUpButton — the real, tappable extra-usage top-up.
 *
 * The Terms of Service name an extra-usage top-up as the PRIMARY option once a
 * managed AI allowance is reached. Until this component existed that promise
 * had no product surface: the backend route (POST /billing/extra-usage/checkout)
 * was live on cloud with zero callers, so a user literally could not buy it
 * (candidate C-432 / #4215).
 *
 * This is the caller. It:
 *   1. reads GET /billing/extra-usage on mount to learn whether the top-up is
 *      actually purchasable on THIS deployment (a configured Stripe price + the
 *      paid_checkout kill switch enabled) — and renders NOTHING when it is not,
 *      so we never ship a button that 503s;
 *   2. on tap, POSTs /billing/extra-usage/checkout (the path the offer itself
 *      hands back, so the click can never drift from the mounted route) and
 *      opens the returned Stripe Checkout URL in a new tab — the same handoff
 *      pattern the plan-checkout and billing-portal buttons use.
 *
 * PAY-26 note: the amount rendered is the PRODUCT PRICE of the top-up, which a
 * user must see before being sent to a payment page. It is NOT a usage-dollar
 * figure (the credit granted, the allowance, or spend), which PAY-26 forbids
 * revealing — this component never shows any of those.
 */
import { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { apiFetch } from '../hooks/useViolaApi';
import { THEME } from '../config';

/**
 * Render a whole-cent price as a plain dollar string (1000 -> "$10",
 * 1050 -> "$10.50"). Returns '' when the value is not a positive integer.
 * @param {number} cents
 * @returns {string}
 */
export function formatPrice(cents) {
  if (typeof cents !== 'number' || !Number.isFinite(cents) || cents <= 0) return '';
  const dollars = cents / 100;
  const text = Number.isInteger(dollars) ? String(dollars) : dollars.toFixed(2);
  return `$${text}`;
}

const ExtraUsageTopUpButton = ({ returnUrl }) => {
  const [offer, setOffer] = useState(null);
  const [purchasing, setPurchasing] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch('/billing/extra-usage');
        if (!cancelled) setOffer(data || null);
      } catch {
        // Offer unreachable / unavailable: leave `offer` null so no buy action
        // renders. A capped user still has the "Upgrade your plan" route.
        if (!cancelled) setOffer(null);
      }
    })();
    return () => {
      cancelled = true;
      mountedRef.current = false;
    };
  }, []);

  const purchaseUrl = offer && typeof offer.purchase_url === 'string' ? offer.purchase_url : '';
  const available = Boolean(offer && offer.available && purchaseUrl);
  if (!available) return null;

  const priceLabel = formatPrice(offer.price_cents);

  const handleBuy = async () => {
    if (purchasing) return;
    setPurchasing(true);
    try {
      const result = await apiFetch(purchaseUrl, {
        method: 'POST',
        body: JSON.stringify(returnUrl ? { return_url: returnUrl } : {}),
      });
      const url = result?.checkout_url || result?.data?.checkout_url;
      if (url) {
        // Hand off to Stripe Checkout in a new tab, same as the plan-checkout
        // and billing-portal buttons.
        window.open(url, '_blank', 'noopener,noreferrer');
      }
    } catch {
      // apiFetch already logs the failure; the button re-enables so the user
      // can retry.
    } finally {
      if (mountedRef.current) setPurchasing(false);
    }
  };

  return (
    <button
      type="button"
      data-testid="extra-usage-topup"
      onClick={handleBuy}
      disabled={purchasing}
      style={{
        padding: '7px 16px',
        borderRadius: '18px',
        border: `1px solid ${THEME.colors.accentBorder}`,
        backgroundColor: THEME.colors.accentSubtle,
        color: THEME.colors.textPrimary,
        fontSize: '13px',
        fontWeight: 600,
        cursor: purchasing ? 'progress' : 'pointer',
        opacity: purchasing ? 0.7 : 1,
      }}
    >
      {purchasing
        ? 'Opening checkout...'
        : priceLabel
          ? `Add extra usage (${priceLabel})`
          : 'Add extra usage'}
    </button>
  );
};

ExtraUsageTopUpButton.propTypes = {
  // Where Stripe returns the user after checkout. Optional; the backend applies
  // a safe default when omitted.
  returnUrl: PropTypes.string,
};

ExtraUsageTopUpButton.defaultProps = {
  returnUrl: null,
};

export default ExtraUsageTopUpButton;
