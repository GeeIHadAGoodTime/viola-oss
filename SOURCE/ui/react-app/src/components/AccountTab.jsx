/**
 * Account Tab Component for Settings Modal.
 *
 * Provides:
 * - Login / Register forms
 * - User profile display
 * - Subscription status
 * - OAuth provider connections
 * - Multi-room sync settings
 */

import React, { useState, useCallback, useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { useAuth } from '../hooks/useAuth';
import { useAuth as useCloudAuth } from '../auth/useAuth';
import { enabledAuthProviders } from '../auth/authProviders';
import { isDesktopApp } from '../utils/runtimeSurface';
import { decodePlanFromUser } from '../lib/auth_context';
import { useCalendarProviders } from '../hooks/useCalendarProviders';
import { useUsage } from '../hooks/useUsage';
import { apiFetch } from '../hooks/useViolaApi';
import { THEME as theme } from '../config';
import { ACCEPTED_PRIVACY_VERSION, ACCEPTED_TERMS_VERSION } from '../auth/legalVersions';
import ToastContainer, { useToast } from './Toast';
import {
  DangerButton,
  SectionDivider,
  SettingRow,
  Toggle,
} from './settings';

// Icons
const Icons = {
  User: () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="8" r="4"/>
      <path d="M20 21a8 8 0 1 0-16 0"/>
    </svg>
  ),
  Email: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="4" width="20" height="16" rx="2"/>
      <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>
    </svg>
  ),
  Lock: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3" y="11" width="18" height="11" rx="2"/>
      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
    </svg>
  ),
  Google: () => (
    <svg width="20" height="20" viewBox="0 0 24 24">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
      <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
    </svg>
  ),
  Apple: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
    </svg>
  ),
  Crown: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M5 16L3 5l5.5 5L12 4l3.5 6L21 5l-2 11H5zm14 3c0 .6-.4 1-1 1H6c-.6 0-1-.4-1-1v-1h14v1z"/>
    </svg>
  ),
  Sync: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
    </svg>
  ),
  Devices: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="5" y="2" width="14" height="20" rx="2"/>
      <line x1="12" y1="18" x2="12" y2="18"/>
    </svg>
  ),
  MagicWand: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72Z"/>
      <path d="m14 7 3 3"/>
      <path d="M5 6v4"/><path d="M19 14v4"/><path d="M10 2v2"/><path d="M7 8H3"/><path d="M21 16h-4"/><path d="M11 3H9"/>
    </svg>
  ),
  Calendar: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <rect x="3" y="4" width="18" height="18" rx="2"/>
      <line x1="16" y1="2" x2="16" y2="6"/>
      <line x1="8" y1="2" x2="8" y2="6"/>
      <line x1="3" y1="10" x2="21" y2="10"/>
    </svg>
  ),
};

const SUBSCRIPTION_PORTAL_UNAVAILABLE = 'Subscription management is temporarily unavailable.';

// Section Component
const Section = ({ title, children }) => (
  <div style={{ marginBottom: '28px' }}>
    <div style={{
      fontSize: '11px',
      fontWeight: 600,
      textTransform: 'uppercase',
      letterSpacing: '1px',
      color: theme.colors.textMuted,
      marginBottom: '12px',
      paddingLeft: '20px',
    }}>
      {title}
    </div>
    <div style={{
      backgroundColor: theme.colors.bgElevated,
      borderRadius: '16px',
      border: `1px solid ${theme.colors.borderSubtle}`,
      overflow: 'hidden',
    }}>
      {children}
    </div>
  </div>
);

Section.propTypes = {
  title: PropTypes.string.isRequired,
  children: PropTypes.node.isRequired,
};

// Input Component
const Input = ({ type = 'text', placeholder, value, onChange, icon: Icon, ...inputProps }) => (
  <div style={{ position: 'relative' }}>
    {Icon && (
      <div style={{
        position: 'absolute',
        left: '16px',
        top: '50%',
        transform: 'translateY(-50%)',
        color: theme.colors.textMuted,
      }}>
        <Icon />
      </div>
    )}
    <input
      type={type}
      placeholder={placeholder}
      value={value}
      onChange={onChange}
      {...inputProps}
      style={{
        width: '100%',
        padding: Icon ? '14px 16px 14px 48px' : '14px 16px',
        borderRadius: '12px',
        border: `1px solid ${theme.colors.borderLight}`,
        backgroundColor: theme.colors.bgCard,
        color: theme.colors.textPrimary,
        fontSize: '14px',
        outline: 'none',
        boxSizing: 'border-box',
        transition: 'border-color 0.15s ease',
      }}
      onFocus={(e) => e.target.style.borderColor = theme.colors.borderHover}
      onBlur={(e) => e.target.style.borderColor = theme.colors.borderLight}
    />
  </div>
);

Input.propTypes = {
  type: PropTypes.string,
  placeholder: PropTypes.string,
  value: PropTypes.string,
  onChange: PropTypes.func,
  icon: PropTypes.elementType,
};

// Button Component
const Button = ({ variant = 'primary', type = 'button', onClick, disabled, children, icon: Icon, fullWidth }) => {
  const [hovered, setHovered] = useState(false);
  const isPrimary = variant === 'primary';

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        width: fullWidth ? '100%' : 'auto',
        padding: '14px 24px',
        borderRadius: '12px',
        border: isPrimary ? 'none' : `1px solid ${hovered ? theme.colors.borderHover : theme.colors.borderLight}`,
        backgroundColor: isPrimary
          ? theme.colors.accent
          : (hovered ? theme.colors.glassHover : 'transparent'),
        color: isPrimary ? '#fff' : theme.colors.textPrimary,
        fontSize: '14px',
        fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'all 0.15s ease',
        opacity: disabled ? 0.5 : 1,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '10px',
        boxShadow: isPrimary && hovered ? `0 0 0 3px ${theme.colors.accentRing}` : 'none',
      }}
    >
      {Icon && <Icon />}
      {children}
    </button>
  );
};

Button.propTypes = {
  variant: PropTypes.oneOf(['primary', 'secondary', 'oauth']),
  type: PropTypes.string,
  onClick: PropTypes.func,
  disabled: PropTypes.bool,
  children: PropTypes.node.isRequired,
  icon: PropTypes.elementType,
  fullWidth: PropTypes.bool,
};

const Spinner = () => (
  <>
    <style>
      {'@keyframes account-tab-spin { to { transform: rotate(360deg); } }'}
    </style>
    <span
      aria-hidden="true"
      style={{
        width: '14px',
        height: '14px',
        border: '2px solid currentColor',
        borderTopColor: 'transparent',
        borderRadius: '50%',
        display: 'inline-block',
        animation: 'account-tab-spin 0.75s linear infinite',
      }}
    />
  </>
);

// Divider
const Divider = ({ text }) => (
  <div style={{
    display: 'flex',
    alignItems: 'center',
    gap: '16px',
    margin: '20px 0',
  }}>
    <div style={{ flex: 1, height: '1px', backgroundColor: theme.colors.borderLight }} />
    {text && <span style={{ color: theme.colors.textMuted, fontSize: '12px' }}>{text}</span>}
    <div style={{ flex: 1, height: '1px', backgroundColor: theme.colors.borderLight }} />
  </div>
);

Divider.propTypes = {
  text: PropTypes.string,
};

// Format an ISO timestamp like "2026-06-02T03:08:12Z" → "Jun 2"
function formatResetDate(isoString) {
  if (!isoString) return null;
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function pickPercentColor(percent) {
  if (percent >= 90) return theme.colors.statusRed;
  if (percent >= 70) return theme.colors.statusYellow;
  return theme.colors.statusGreen;
}

// Spend usage row — "X% used this <window>, resets <date>".
// Hidden when there's no configured limit (Max plan, BYOK, etc).
const UsageRow = ({ label, percent, resetsAt }) => {
  const color = pickPercentColor(percent);
  const resetLabel = formatResetDate(resetsAt);
  return (
    <div
      data-testid={`usage-row-${label}`}
      style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ color: theme.colors.textPrimary, fontSize: '13px', fontWeight: 500 }}>
          {`${percent}% used this ${label}`}
        </span>
        {resetLabel && (
          <span style={{ color: theme.colors.textMuted, fontSize: '12px' }}>
            {`resets ${resetLabel}`}
          </span>
        )}
      </div>
      <div
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label} usage`}
        style={{
          width: '100%',
          height: '6px',
          borderRadius: '4px',
          backgroundColor: theme.colors.glassBase,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${percent}%`,
            height: '100%',
            backgroundColor: color,
            transition: 'width 0.4s ease',
          }}
        />
      </div>
    </div>
  );
};

UsageRow.propTypes = {
  label: PropTypes.oneOf(['week', 'month']).isRequired,
  percent: PropTypes.number.isRequired,
  resetsAt: PropTypes.string,
};

const UsageSummary = () => {
  const { usage } = useUsage();
  if (!usage) return null;
  const showMonthly = typeof usage.monthlyPercent === 'number';
  const showWeekly = typeof usage.weeklyPercent === 'number';
  if (!showMonthly && !showWeekly) return null;

  return (
    <div
      style={{
        marginTop: '16px',
        padding: '14px 16px',
        borderRadius: '12px',
        backgroundColor: theme.colors.bgCard,
        border: `1px solid ${theme.colors.borderLight}`,
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}
    >
      {showMonthly && (
        <UsageRow
          label="month"
          percent={usage.monthlyPercent}
          resetsAt={usage.monthlyResetsAt}
        />
      )}
      {showWeekly && (
        <UsageRow
          label="week"
          percent={usage.weeklyPercent}
          resetsAt={usage.weeklyResetsAt}
        />
      )}
    </div>
  );
};

const CHECKOUT_UNAVAILABLE = 'Checkout is temporarily unavailable. Please try again shortly.';

// Reuse the website's post-payment confirmation page as the Stripe return
// target so the browser tab that runs Checkout lands on the same success /
// cancel screens the website flow already uses. Both URLs are on the trusted
// useviola.com domain (core.url_validation.validate_return_url allows it).
const CHECKOUT_SUCCESS_URL =
  'https://useviola.com/checkout.html?status=success&provider=stripe&session_id={CHECKOUT_SESSION_ID}';
const CHECKOUT_CANCEL_URL = 'https://useviola.com/checkout.html?status=canceled';
// Full checkout flow on the website, used only as a graceful fallback when the
// in-app plan catalog cannot be loaded. This is the real checkout (its
// checkout.js POSTs /billing/checkout), never the marketing /pricing page.
const WEBSITE_CHECKOUT_URL = 'https://useviola.com/checkout.html';

function formatPlanPrice(priceCents, interval) {
  const amount = `$${(Number(priceCents || 0) / 100).toFixed(2)}`;
  return `${amount}${interval === 'year' ? '/yr' : '/mo'}`;
}

// ROSCA / Auto-Renewal-Law refund disclosure, matched to the plan interval and
// kept consistent with ViolaWebsite/js/checkout.js.
function planRefundSummary(interval) {
  return interval === 'year'
    ? 'Annual plans can be refunded pro-rated if you cancel within the first 14 days of a subscription or renewal.'
    : 'Monthly plans are not refunded for the current billing cycle once it has started.';
}

// In-app upgrade flow. Instead of dead-ending at the marketing pricing page,
// this initiates the REAL Stripe checkout via the backend /billing/checkout
// route using the signed-in user's existing session (same handoff pattern as
// Manage Subscription's portal button), then refreshes entitlement when the
// user returns so the paid plan shows without a manual reload / re-login
// (#2609). ROSCA affirmative consent is captured before payment can begin.
const UpgradePanel = ({ addToast, refreshUser }) => {
  const [plans, setPlans] = useState(null); // null = loading, [] = none/failed
  const [plansError, setPlansError] = useState(false);
  const [selectedPlanId, setSelectedPlanId] = useState(null);
  const [consent, setConsent] = useState(false);
  const [consentError, setConsentError] = useState(false);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [error, setError] = useState(null);
  const checkoutInFlightRef = useRef(false);
  // True while a post-checkout entitlement poll loop is already running, so a
  // single return to the tab starts exactly one. Held in a ref rather than a
  // closure variable so it still holds if this effect re-subscribes mid-poll.
  const entitlementPollActiveRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Load the paid plan catalog from the backend so prices/intervals are never
  // hardcoded — they come from billing/pricing via /billing/plans.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const data = await apiFetch('/billing/plans');
        const all = Array.isArray(data?.plans) ? data.plans : [];
        const paid = all.filter((plan) => Number(plan?.price_cents) > 0);
        if (!active) return;
        setPlans(paid);
        const preferred = paid.find((plan) => plan.id === 'pro_monthly') || paid[0] || null;
        setSelectedPlanId(preferred ? preferred.id : null);
      } catch {
        if (active) {
          setPlans([]);
          setPlansError(true);
        }
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // When the tab regains focus/visibility after a checkout was initiated, pull
  // fresh entitlement so a completed upgrade shows without a manual reload. The
  // Stripe webhook may take a moment to write the new plan to app_metadata, so
  // refresh a few times with a short, bounded backoff.
  useEffect(() => {
    const onReturn = () => {
      if (!checkoutInFlightRef.current) return;
      if (document.visibilityState === 'hidden') return;
      // Both listeners below answer the SAME return to the tab, so without
      // this guard one checkout return starts two overlapping poll loops and
      // doubles the grants issued against the one rotating refresh token the
      // cloud front door owns. Two listeners are still registered because
      // browsers do not agree on which one fires: switching tabs within a
      // window raises `visibilitychange`, switching windows raises `focus`.
      if (entitlementPollActiveRef.current) return;
      entitlementPollActiveRef.current = true;
      let attempts = 0;
      const poll = async () => {
        attempts += 1;
        try {
          await refreshUser();
        } catch {
          /* entitlement refresh is best-effort */
        }
        if (attempts < 5 && checkoutInFlightRef.current) {
          setTimeout(poll, 2000);
        } else {
          entitlementPollActiveRef.current = false;
        }
      };
      poll();
    };
    document.addEventListener('visibilitychange', onReturn);
    window.addEventListener('focus', onReturn);
    return () => {
      document.removeEventListener('visibilitychange', onReturn);
      window.removeEventListener('focus', onReturn);
    };
  }, [refreshUser]);

  const selectedPlan = plans?.find((plan) => plan.id === selectedPlanId) || null;

  const openWebsiteCheckout = () => {
    checkoutInFlightRef.current = true;
    window.open(WEBSITE_CHECKOUT_URL, '_blank', 'noopener,noreferrer');
  };

  const handleUpgrade = async () => {
    if (!selectedPlan) return;
    if (!consent) {
      setConsentError(true);
      return;
    }
    setConsentError(false);
    setError(null);
    setCheckoutLoading(true);
    try {
      const result = await apiFetch('/billing/checkout', {
        method: 'POST',
        body: JSON.stringify({
          plan_id: selectedPlan.id,
          provider: 'stripe',
          return_url: CHECKOUT_SUCCESS_URL,
          cancel_url: CHECKOUT_CANCEL_URL,
          terms_accepted: true,
          terms_version: ACCEPTED_TERMS_VERSION,
          privacy_version: ACCEPTED_PRIVACY_VERSION,
        }),
      });
      const url = result?.checkout_url || result?.data?.checkout_url;
      if (!url) {
        throw new Error('Missing checkout URL');
      }
      // Mark checkout in-flight so the return listener refreshes entitlement
      // when the user comes back, then hand off to Stripe Checkout in a new tab
      // (same pattern as Manage Subscription's portal handoff).
      checkoutInFlightRef.current = true;
      window.open(url, '_blank', 'noopener,noreferrer');
    } catch {
      if (mountedRef.current) {
        setError(CHECKOUT_UNAVAILABLE);
        addToast({ message: CHECKOUT_UNAVAILABLE, level: 'error' });
      }
    } finally {
      if (mountedRef.current) setCheckoutLoading(false);
    }
  };

  // Plan catalog could not be loaded — fall back to the website's full checkout
  // flow rather than dead-ending. Still the real /billing/checkout path.
  if (plansError || (plans && plans.length === 0)) {
    return (
      <Button variant="secondary" fullWidth onClick={openWebsiteCheckout}>
        <Icons.Crown /> Upgrade Plan
      </Button>
    );
  }

  // Catalog still loading.
  if (plans === null) {
    return (
      <Button variant="secondary" fullWidth disabled>
        <Spinner /> Loading plans...
      </Button>
    );
  }

  const interval = selectedPlan?.interval || 'month';

  return (
    <div
      data-testid="upgrade-panel"
      style={{
        marginTop: '8px',
        padding: '16px',
        borderRadius: '12px',
        backgroundColor: theme.colors.bgCard,
        border: `1px solid ${theme.colors.borderLight}`,
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}
    >
      <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 600 }}>
        Upgrade your plan
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {plans.map((plan) => {
          const isSelected = plan.id === selectedPlanId;
          return (
            <button
              key={plan.id}
              type="button"
              onClick={() => setSelectedPlanId(plan.id)}
              aria-pressed={isSelected}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '12px',
                padding: '12px 14px',
                borderRadius: '10px',
                border: `1px solid ${isSelected ? theme.colors.accent : theme.colors.borderLight}`,
                backgroundColor: isSelected ? `${theme.colors.accent}1A` : 'transparent',
                color: theme.colors.textPrimary,
                cursor: 'pointer',
                textAlign: 'left',
                transition: 'all 0.15s ease',
              }}
            >
              <span style={{ fontSize: '14px', fontWeight: 500 }}>{plan.name}</span>
              <span style={{ fontSize: '13px', color: theme.colors.textMuted }}>
                {formatPlanPrice(plan.price_cents, plan.interval)}
              </span>
            </button>
          );
        })}
      </div>

      {/* ROSCA / Auto-Renewal-Law: auto-renew + refund disclosure. */}
      <p style={{ margin: 0, color: theme.colors.textMuted, fontSize: '12px', lineHeight: 1.5 }}>
        Your subscription auto-renews at {selectedPlan ? formatPlanPrice(selectedPlan.price_cents, interval) : 'the plan price'}{' '}
        until you cancel. You can cancel anytime in Manage Subscription or by emailing{' '}
        <a href="mailto:support@useviola.com" style={{ color: theme.colors.accent }}>support@useviola.com</a>.{' '}
        {planRefundSummary(interval)} See the{' '}
        <a href="https://useviola.com/terms" target="_blank" rel="noopener noreferrer" style={{ color: theme.colors.accent }}>
          Terms of Service
        </a>{' '}for the full refund policy.
      </p>

      <label style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', cursor: 'pointer', fontSize: '13px', color: theme.colors.textMuted, lineHeight: 1.4 }}>
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => {
            setConsent(e.target.checked);
            if (e.target.checked) setConsentError(false);
          }}
          aria-label="Agree to the Terms of Service and Privacy Policy and consent to the recurring charge"
          style={{ width: '18px', height: '18px', marginTop: '1px', accentColor: theme.colors.accent, flexShrink: 0 }}
        />
        <span>
          I have read and agree to the{' '}
          <a href="https://useviola.com/terms" target="_blank" rel="noopener noreferrer" style={{ color: theme.colors.accent }}>Terms of Service</a>{' '}and{' '}
          <a href="https://useviola.com/privacy" target="_blank" rel="noopener noreferrer" style={{ color: theme.colors.accent }}>Privacy Policy</a>,
          and I consent to the recurring charge described above.
        </span>
      </label>

      {consentError && (
        <div style={{ color: theme.colors.statusRed, fontSize: '12px' }}>
          Please agree to the Terms of Service and Privacy Policy to continue.
        </div>
      )}

      {error && (
        <div style={{ color: theme.colors.statusRed, fontSize: '13px' }}>{error}</div>
      )}

      <Button
        variant="primary"
        fullWidth
        onClick={handleUpgrade}
        disabled={checkoutLoading || !selectedPlan || !consent}
        icon={Icons.Crown}
      >
        {checkoutLoading && <Spinner />}
        {checkoutLoading ? 'Opening checkout...' : 'Upgrade Plan'}
      </Button>
    </div>
  );
};

UpgradePanel.propTypes = {
  addToast: PropTypes.func.isRequired,
  refreshUser: PropTypes.func.isRequired,
};

// User Profile Card
const ProfileCard = ({ user, subscription, onLogout, addToast, refreshUser }) => {
  const [portalLoading, setPortalLoading] = useState(false);
  const canManageSubscription = Boolean(
    subscription?.hasPaidAccess && subscription?.paymentProvider === 'stripe',
  );

  const handleManageSubscription = async () => {
    setPortalLoading(true);
    try {
      const data = await apiFetch('/v1/billing/portal/session', { method: 'POST' });
      if (!data?.url) {
        throw new Error('Missing billing portal URL');
      }
      window.open(data.url, '_blank', 'noopener,noreferrer');
    } catch {
      addToast({ message: SUBSCRIPTION_PORTAL_UNAVAILABLE, level: 'error' });
    } finally {
      setPortalLoading(false);
    }
  };

  return (
    <div style={{ padding: '24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '20px' }}>
        <div style={{
          width: '64px',
          height: '64px',
          borderRadius: '50%',
          backgroundColor: theme.colors.accent,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: theme.colors.textBright,
          fontSize: '24px',
          fontWeight: 600,
        }}>
          {user.email?.charAt(0).toUpperCase() || 'U'}
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ color: theme.colors.textPrimary, fontSize: '18px', fontWeight: 600 }}>
            {user.email}
          </div>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            marginTop: '4px',
          }}>
            {subscription?.hasPaidAccess && (
              <span style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                padding: '4px 10px',
                borderRadius: '20px',
                backgroundColor: `${theme.colors.statusYellow}33`,
                color: theme.colors.statusYellow,
                fontSize: '12px',
                fontWeight: 600,
              }}>
                <Icons.Crown /> {subscription?.planFamily === 'max' ? 'Max' : 'Pro'}
              </span>
            )}
            {!subscription?.hasPaidAccess && (
              <span style={{
                padding: '4px 10px',
                borderRadius: '20px',
                backgroundColor: theme.colors.glassBase,
                color: theme.colors.textMuted,
                fontSize: '12px',
                fontWeight: 500,
              }}>
                Free Plan
              </span>
            )}
          </div>
        </div>
      </div>

      <UsageSummary />

      {canManageSubscription && (
        <Button
          variant="secondary"
          fullWidth
          onClick={handleManageSubscription}
          disabled={portalLoading}
        >
          {portalLoading && <Spinner />}
          {portalLoading ? 'Opening...' : 'Manage Subscription'}
        </Button>
      )}

      {!subscription?.hasPaidAccess && (
        <UpgradePanel addToast={addToast} refreshUser={refreshUser} />
      )}

      <div style={{ marginTop: '16px' }}>
        <Button variant="secondary" fullWidth onClick={onLogout}>
          Sign Out
        </Button>
      </div>
    </div>
  );
};

ProfileCard.propTypes = {
  user: PropTypes.shape({
    email: PropTypes.string,
  }).isRequired,
  subscription: PropTypes.shape({
    status: PropTypes.string,
    planFamily: PropTypes.string,
    hasPaidAccess: PropTypes.bool,
    paymentProvider: PropTypes.string,
  }),
  onLogout: PropTypes.func.isRequired,
  addToast: PropTypes.func.isRequired,
  refreshUser: PropTypes.func.isRequired,
};

// Login/Register Form
const AuthForm = ({ onSuccess }) => {
  // 'login' | 'register' | 'magic' | 'forgot-password' | 'code' | 'reset-confirm' | 'mfa'
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [code, setCode] = useState('');
  const [mfaFactorId, setMfaFactorId] = useState(null);
  const [legalEligibilityConfirmed, setLegalEligibilityConfirmed] = useState(false);
  const [tosAccepted, setTosAccepted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState(null);
  const [localError, setLocalError] = useState(null);
  const [showErrorDetails, setShowErrorDetails] = useState(false);
  const [oauthProviders, setOauthProviders] = useState([]);

  const {
    login,
    verifyMfaTotp,
    register,
    requestMagicLink,
    verifyEmailOtp,
    requestPasswordReset,
    updatePassword,
    startOAuthFlow,
    logout,
    passwordRecovery,
    error,
    errorDetails,
    clearError,
  } = useAuth();
  // 'code' = Path C2 — user types the 8-char short code from the magic-link
  // email instead of clicking the URL. 'magic' / 'forgot-password' don't need
  // a password input either. 'mfa' = TOTP second factor (6-digit code) after a
  // password sign-in on an MFA-enrolled account; no email/password input.
  const isPasswordlessMode = mode === 'magic' || mode === 'forgot-password' || mode === 'code' || mode === 'mfa';

  // Ask the auth service which providers it accepts. Desktop only: the flow
  // needs a loopback listener AND a page that never navigates away (the PKCE
  // verifier lives in this tab's heap), neither of which a browser surface has.
  useEffect(() => {
    let cancelled = false;
    if (!isDesktopApp()) return undefined;
    enabledAuthProviders().then((providers) => {
      if (!cancelled) setOauthProviders(providers);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (passwordRecovery) {
      setMode('reset-confirm');
      setPassword('');
      setConfirmPassword('');
      setMessage(null);
      setLocalError(null);
    }
  }, [passwordRecovery]);

  const switchMode = (nextMode) => {
    setMode(nextMode);
    setMessage(null);
    setLocalError(null);
    setShowErrorDetails(false);
    setLegalEligibilityConfirmed(false);
    setTosAccepted(false);
    setCode('');
    setConfirmPassword('');
    setMfaFactorId(null);
    clearError();
  };

  // Cancel out of the TOTP step: the password sign-in already minted an AAL1
  // GoTrue session, so drop it (sign out) before returning to the login form —
  // otherwise a half-authenticated session would linger.
  const cancelMfa = async () => {
    setSubmitting(true);
    try {
      await logout();
    } finally {
      setSubmitting(false);
      setPassword('');
      switchMode('login');
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setMessage(null);
    clearError();

    setLocalError(null);
    setShowErrorDetails(false);

    try {
      let result;
      if (mode === 'mfa') {
        result = await verifyMfaTotp(mfaFactorId, code);
        if (result.success) {
          onSuccess?.();
        } else {
          setLocalError(result.error);
          setShowErrorDetails(Boolean(result.errorDetails));
        }
      } else if (mode === 'code') {
        result = await verifyEmailOtp(email, code);
        if (result.success) {
          onSuccess?.();
        } else {
          setLocalError(result.error);
          setShowErrorDetails(false);
        }
      } else if (mode === 'magic') {
        result = await requestMagicLink(email);
        if (result.success) {
          setMessage({
            type: 'success',
            text: 'Check your email — click the link or enter the 8-character code below.',
          });
          // Auto-switch into code-entry mode so the user can paste the code
          // straight from the email without backtracking.
          setTimeout(() => switchMode('code'), 800);
        }
      } else if (mode === 'forgot-password') {
        result = await requestPasswordReset(email);
        if (result.success) {
          setMessage({
            type: 'success',
            text: 'If an account exists with that email, you will receive a reset link shortly.',
          });
        } else {
          setMessage({ type: 'error', text: result.error });
        }
      } else if (mode === 'reset-confirm') {
        if (password !== confirmPassword) {
          setLocalError('Passwords do not match.');
          setShowErrorDetails(false);
        } else {
          result = await updatePassword(password);
          if (result.success) {
            setMessage({
              type: 'success',
              text: result.message || 'Password updated.',
            });
            setPassword('');
            setConfirmPassword('');
          } else {
            setLocalError(result.error);
            setShowErrorDetails(Boolean(result.errorDetails));
          }
        }
      } else if (mode === 'register') {
        if (!legalEligibilityConfirmed || !tosAccepted) {
          setLocalError('Confirm eligibility and accept the Terms of Service to create an account.');
          setShowErrorDetails(false);
        } else {
          result = await register(email, password, {
            tosAccepted,
            legalEligibilityConfirmed,
            termsVersion: ACCEPTED_TERMS_VERSION,
            privacyVersion: ACCEPTED_PRIVACY_VERSION,
          });
          if (result.success) {
            // Registration no longer logs user in; show verification message
            // and switch to sign-in mode so user can log in after verifying.
            setMessage({
              type: 'success',
              text: result.message || 'Account created! Check your email to verify, then sign in.',
            });
            setMode('login');
            setPassword('');
          }
        }
      } else {
        result = await login(email, password);
        if (result.success) {
          onSuccess?.();
        } else if (result.mfaRequired) {
          // MFA-enrolled account: password was accepted (AAL1) but a TOTP
          // second factor is required. Switch to the TOTP prompt. switchMode
          // clears mfaFactorId, so set it AFTER (the later setState wins).
          switchMode('mfa');
          setMfaFactorId(result.factorId);
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  // Provider sign-in finishes in the system browser and comes back to the
  // app's loopback listener, so this await lasts as long as the human takes.
  // Say so while it runs: the alternative is a screen that looks idle for a
  // minute and then either changes or does not, with nothing explaining why.
  const handleOAuth = async (provider) => {
    setSubmitting(true);
    setLocalError(null);
    setMessage('Finish signing in with your browser, then come back here.');
    clearError();
    const result = await startOAuthFlow(provider);
    setSubmitting(false);
    setMessage(null);
    if (result.success) {
      onSuccess?.();
      return;
    }
    // startOAuthFlow already published the reason to the shared auth error;
    // hold it locally too so it survives a later clearError().
    if (result.error) setLocalError(result.error);
  };

  const visibleError = error || localError;
  const visibleErrorDetails = errorDetails;

  return (
    <div style={{ padding: '24px' }}>
      <div style={{ textAlign: 'center', marginBottom: '24px' }}>
        <h3 style={{
          color: theme.colors.textPrimary,
          fontSize: '20px',
          fontWeight: 600,
          marginBottom: '8px',
        }}>
          {mode === 'register'
            ? 'Create Account'
            : mode === 'forgot-password'
              ? 'Reset Password'
              : mode === 'reset-confirm'
                ? 'Set New Password'
              : mode === 'code'
                ? 'Enter Sign-In Code'
              : mode === 'mfa'
                ? 'Two-Factor Authentication'
                : 'Sign In'}
        </h3>
        <p style={{ color: theme.colors.textMuted, fontSize: '14px', margin: 0 }}>
          {mode === 'register'
            ? 'Create an account to sync across devices'
            : mode === 'forgot-password'
              ? 'Enter your email and we will send you a reset link'
              : mode === 'reset-confirm'
                ? 'Choose a new password for your Viola account'
              : mode === 'code'
                ? 'Type the 8-character code from your email to sign in.'
              : mode === 'mfa'
                ? 'Enter the 6-digit code from your authenticator app.'
                : 'Sign in to access your Viola account'}
        </p>
      </div>

      {/* Only providers the auth service actually accepts, and only on the
          surface where the flow can complete. See auth/authProviders.ts and
          auth/desktopOAuth.ts — an offered button that cannot work is the bug
          this replaces, not a feature. */}
      {oauthProviders.length > 0 && mode !== 'forgot-password' && mode !== 'reset-confirm' && mode !== 'mfa' && (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginBottom: '20px' }}>
            {oauthProviders.map((provider) => (
              <Button
                key={provider}
                variant="oauth"
                fullWidth
                onClick={() => handleOAuth(provider)}
                disabled={submitting}
                icon={provider === 'google' ? Icons.Google : Icons.Apple}
              >
                {provider === 'google' ? 'Continue with Google' : 'Continue with Apple'}
              </Button>
            ))}
          </div>

          <Divider text="or" />
        </>
      )}

      {/* Email Form */}
      <form onSubmit={handleSubmit}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {mode === 'forgot-password' && (
            <div style={{
              padding: '12px',
              borderRadius: '8px',
              backgroundColor: theme.colors.bgCard,
              border: `1px solid ${theme.colors.borderLight}`,
              color: theme.colors.textMuted,
              fontSize: '13px',
            }}>
              Enter the email address on your account and we will send you a reset link.
            </div>
          )}

          {mode !== 'reset-confirm' && mode !== 'mfa' && (
            <Input
              type="email"
              placeholder={mode === 'forgot-password' ? 'Email address' : 'Email'}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              icon={Icons.Email}
              aria-label={mode === 'forgot-password' ? 'Email address' : 'Email'}
              autoComplete="email"
            />
          )}

          {!isPasswordlessMode && (
            <Input
              type="password"
              placeholder={mode === 'reset-confirm' ? 'New password' : 'Password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              icon={Icons.Lock}
              aria-label="Password"
              autoComplete={mode === 'register' || mode === 'reset-confirm' ? 'new-password' : 'current-password'}
            />
          )}

          {mode === 'reset-confirm' && (
            <Input
              type="password"
              placeholder="Confirm new password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              icon={Icons.Lock}
              aria-label="Confirm new password"
              autoComplete="new-password"
            />
          )}

          {mode === 'code' && (
            <Input
              type="text"
              placeholder="8-character code"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8))}
              icon={Icons.MagicWand}
              aria-label="Sign-in code from email"
              autoComplete="one-time-code"
              inputMode="text"
              maxLength={8}
              style={{ letterSpacing: '0.4em', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', textTransform: 'uppercase' }}
            />
          )}

          {mode === 'mfa' && (
            <Input
              type="text"
              placeholder="6-digit code"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/[^0-9]/g, '').slice(0, 6))}
              icon={Icons.Lock}
              aria-label="Authenticator code"
              autoComplete="one-time-code"
              inputMode="numeric"
              maxLength={6}
              autoFocus
              style={{ letterSpacing: '0.5em', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
            />
          )}

          {mode === 'login' && (
            <button
              type="button"
              onClick={() => switchMode('forgot-password')}
              style={{
                alignSelf: 'flex-end',
                minHeight: '44px',
                padding: '0 4px',
                display: 'inline-flex',
                alignItems: 'center',
                background: 'none',
                border: 'none',
                color: theme.colors.accent,
                fontSize: '13px',
                cursor: 'pointer',
              }}
            >
              Forgot password?
            </button>
          )}

          {mode === 'register' && (
            <div style={{ display: 'grid', gap: '10px' }}>
              <label style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: '10px',
                cursor: 'pointer',
                fontSize: '13px',
                color: theme.colors.textMuted,
                lineHeight: '1.4',
              }}>
                <input
                  type="checkbox"
                  checked={legalEligibilityConfirmed}
                  onChange={(e) => setLegalEligibilityConfirmed(e.target.checked)}
                  style={{
                    width: '18px',
                    height: '18px',
                    marginTop: '1px',
                    accentColor: theme.colors.accent,
                    flexShrink: 0,
                  }}
                />
                <span>I confirm that I meet the age and guardian-consent requirements in the Terms of Service.</span>
              </label>
              <label style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: '10px',
                cursor: 'pointer',
                fontSize: '13px',
                color: theme.colors.textMuted,
                lineHeight: '1.4',
              }}>
                <input
                  type="checkbox"
                  checked={tosAccepted}
                  onChange={(e) => setTosAccepted(e.target.checked)}
                  style={{
                    width: '18px',
                    height: '18px',
                    marginTop: '1px',
                    accentColor: theme.colors.accent,
                    flexShrink: 0,
                  }}
                />
                <span>
                  I agree to the{' '}
                  <a href="https://useviola.com/terms" target="_blank" rel="noopener noreferrer"
                    style={{ color: theme.colors.accent, textDecoration: 'underline' }}>
                    Terms of Service
                  </a>{' '}and{' '}
                  <a href="https://useviola.com/privacy" target="_blank" rel="noopener noreferrer"
                    style={{ color: theme.colors.accent, textDecoration: 'underline' }}>
                    Privacy Policy
                  </a>
                </span>
              </label>
            </div>
          )}

          {visibleError && (
            <div style={{
              padding: '12px',
              borderRadius: '8px',
              backgroundColor: `${theme.colors.statusRed}1A`,
              color: theme.colors.statusRed,
              fontSize: '13px',
            }}>
              <div>{visibleError}</div>
              {visibleErrorDetails?.code && (
                <div style={{ marginTop: '8px' }}>
                  <button
                    type="button"
                    onClick={() => setShowErrorDetails((value) => !value)}
                    style={{
                      padding: 0,
                      background: 'none',
                      border: 'none',
                      color: theme.colors.statusRed,
                      cursor: 'pointer',
                      fontSize: '12px',
                      textDecoration: 'underline',
                    }}
                  >
                    Details
                  </button>
                  {showErrorDetails && (
                    <div style={{
                      marginTop: '6px',
                      color: theme.colors.textMuted,
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                      fontSize: '12px',
                      lineHeight: 1.5,
                      overflowWrap: 'anywhere',
                    }}>
                      code: {visibleErrorDetails.code}
                      {visibleErrorDetails.status ? `; status: ${visibleErrorDetails.status}` : ''}
                      {visibleErrorDetails.retryAfter ? `; retry_after: ${visibleErrorDetails.retryAfter}s` : ''}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {message && (
            <div style={{
              padding: '12px',
              borderRadius: '8px',
              backgroundColor: message.type === 'success' ? `${theme.colors.statusGreen}1A` : `${theme.colors.statusRed}1A`,
              color: message.type === 'success' ? theme.colors.statusGreen : theme.colors.statusRed,
              fontSize: '13px',
            }}>
              {message.text}
            </div>
          )}

          <Button
            type="submit"
            variant="primary"
            fullWidth
            disabled={
              submitting
              || (mode !== 'reset-confirm' && mode !== 'mfa' && !email.trim())
              || (!isPasswordlessMode && !password.trim())
              || (mode === 'reset-confirm' && (!confirmPassword.trim() || password !== confirmPassword))
              || (mode === 'code' && code.trim().length !== 8)
              || (mode === 'mfa' && code.trim().length !== 6)
              || (mode === 'register' && (!legalEligibilityConfirmed || !tosAccepted))
            }
          >
            {submitting ? 'Please wait...' : (
              mode === 'magic' ? 'Send Magic Link' :
              mode === 'forgot-password' ? 'Send Reset Link' :
              mode === 'reset-confirm' ? 'Update Password' :
              mode === 'code' ? 'Sign In with Code' :
              mode === 'mfa' ? 'Verify' :
              mode === 'register' ? 'Create Account' : 'Sign In'
            )}
          </Button>
        </div>
      </form>

      {/* Mode Switcher */}
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '12px',
        marginTop: '20px',
      }}>
        {mode === 'login' && (
          <>
            <button
              type="button"
              onClick={() => switchMode('magic')}
              aria-label="Sign in with magic link"
              style={{
                background: 'none',
                border: 'none',
                color: theme.colors.accent,
                fontSize: '14px',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                minHeight: '44px',
                padding: '0 8px',
                gap: '6px',
              }}
            >
              <Icons.MagicWand /> Sign in with magic link
            </button>
            <span style={{ color: theme.colors.textMuted, fontSize: '14px', display: 'inline-flex', alignItems: 'center', minHeight: '44px', flexWrap: 'wrap', justifyContent: 'center' }}>
              Don't have an account?{' '}
              <button
                type="button"
                onClick={() => switchMode('register')}
                style={{
                  background: 'none',
                  border: 'none',
                  color: theme.colors.textPrimary,
                  fontSize: '14px',
                  cursor: 'pointer',
                  fontWeight: 500,
                  minHeight: '44px',
                  padding: '0 6px',
                }}
              >
                Sign up
              </button>
            </span>
          </>
        )}
        {mode === 'register' && (
          <span style={{ color: theme.colors.textMuted, fontSize: '14px' }}>
            Already have an account?{' '}
            <button
              type="button"
              onClick={() => switchMode('login')}
              style={{
                background: 'none',
                border: 'none',
                color: theme.colors.textPrimary,
                fontSize: '14px',
                cursor: 'pointer',
                fontWeight: 500,
              }}
            >
              Sign in
            </button>
          </span>
        )}
        {mode === 'mfa' && (
          <button
            type="button"
            onClick={cancelMfa}
            disabled={submitting}
            style={{
              background: 'none',
              border: 'none',
              color: theme.colors.textMuted,
              fontSize: '14px',
              cursor: submitting ? 'not-allowed' : 'pointer',
            }}
          >
            Cancel and sign in again
          </button>
        )}
        {(mode === 'magic' || mode === 'forgot-password' || mode === 'code') && (
          <>
            {(mode === 'magic') && (
              <button
                type="button"
                onClick={() => switchMode('code')}
                style={{
                  background: 'none',
                  border: 'none',
                  color: theme.colors.accent,
                  fontSize: '14px',
                  cursor: 'pointer',
                }}
              >
                I have a code from my email
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                setPassword('');
                switchMode('login');
              }}
              style={{
                background: 'none',
                border: 'none',
                color: theme.colors.textMuted,
                fontSize: '14px',
                cursor: 'pointer',
              }}
            >
              Back to sign in
            </button>
          </>
        )}
      </div>
    </div>
  );
};

AuthForm.propTypes = {
  onSuccess: PropTypes.func,
};

// Cloud Sync Status. `signedIn` distinguishes the disabled copy: a
// signed-in-but-not-paid user needs "upgrade" copy, not "sign in" copy — the
// only current caller renders this exclusively from the signed-in branch of
// AccountTab, so a hardcoded signed-out message was always wrong for them.
const SyncStatus = ({ enabled, signedIn = true }) => (
  <div style={{
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '16px 20px',
  }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
      <div style={{
        width: '40px',
        height: '40px',
        borderRadius: '10px',
        backgroundColor: theme.colors.bgCard,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: enabled ? theme.colors.statusGreen : theme.colors.textMuted,
      }}>
        <Icons.Sync />
      </div>
      <div>
        <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 500 }}>
          Cloud Sync
        </div>
        <div style={{ color: enabled ? theme.colors.statusGreen : theme.colors.textMuted, fontSize: '13px' }}>
          {enabled
            ? 'Synced across your devices'
            : signedIn
              ? 'Upgrade to sync across your devices'
              : 'Sign in to sync across your devices'}
        </div>
      </div>
    </div>
    <div style={{
      padding: '6px 12px',
      borderRadius: '6px',
      backgroundColor: enabled ? `${theme.colors.statusGreen}26` : theme.colors.glassBase,
      color: enabled ? theme.colors.statusGreen : theme.colors.textMuted,
      fontSize: '12px',
      fontWeight: 500,
    }}>
      {enabled ? 'Active' : 'Disabled'}
    </div>
  </div>
);

SyncStatus.propTypes = {
  enabled: PropTypes.bool.isRequired,
  signedIn: PropTypes.bool,
};

// Calendar Connection Settings
const isGoogleCalendarSyncConnected = (statusData) => {
  const providers = statusData?.providers ?? statusData?.data?.providers ?? [];
  return providers.some((provider) => (
    (provider.provider === 'google' || provider.provider_id === 'google_calendar' || provider.id === 'google_calendar')
    && provider.configured === true
  ));
};

export const CalendarSettings = () => {
  const { user } = useAuth();
  const { connect, disconnect } = useCalendarProviders();
  const [confirmDisconnect, setConfirmDisconnect] = useState(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const [actionHovered, setActionHovered] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);
  const [calendarConnected, setCalendarConnected] = useState(null); // null = loading
  const mountedRef = useRef(true);

  // Check real calendar connection status from the backend
  const checkCalendarStatus = useCallback(async () => {
    try {
      const data = await apiFetch('/v1/calendar/status');
      const connected = isGoogleCalendarSyncConnected(data);
      if (mountedRef.current) setCalendarConnected(connected);
      return connected;
    } catch {
      if (mountedRef.current) setCalendarConnected(false);
      return false;
    }
  }, []);

  // Check status on mount
  useEffect(() => {
    mountedRef.current = true;
    checkCalendarStatus();
    return () => { mountedRef.current = false; };
  }, [checkCalendarStatus]);

  const isConnected = calendarConnected === true;
  const loading = calendarConnected === null;
  const accountEmail = user?.email || null;

  const handleConnect = useCallback(async () => {
    setError(null);
    setConnecting(true);
    try {
      const result = await connect('google_calendar');
      if (result?.success) {
        // After OAuth login, poll calendar status until connected (max 10s)
        for (let i = 0; i < 10; i++) {
          await new Promise(r => setTimeout(r, 1000));
          const connected = await checkCalendarStatus();
          if (connected) break;
        }
      } else {
        setError(result?.error || 'Sign-in failed. Please try again.');
      }
    } catch {
      setError('Something went wrong. Please try again.');
    } finally {
      if (mountedRef.current) setConnecting(false);
    }
  }, [connect, checkCalendarStatus]);

  const handleDisconnectConfirm = useCallback(async () => {
    if (!confirmDisconnect) return;
    setDisconnecting(true);
    try {
      await disconnect(confirmDisconnect);
      await checkCalendarStatus();
      setConfirmDisconnect(null);
    } catch {
      setError('Failed to disconnect. Please try again.');
    } finally {
      setDisconnecting(false);
    }
  }, [disconnect, confirmDisconnect, checkCalendarStatus]);

  if (loading) {
    return (
      <div style={{
        padding: '16px 20px',
        display: 'flex',
        alignItems: 'center',
        gap: '14px',
      }}>
        <div style={{
          width: '40px',
          height: '40px',
          borderRadius: '10px',
          backgroundColor: theme.colors.bgCard,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: theme.colors.textMuted,
        }}>
          <Icons.Calendar />
        </div>
        <div style={{ color: theme.colors.textMuted, fontSize: '14px' }}>
          Loading...
        </div>
      </div>
    );
  }

  return (
    <div>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '16px 20px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flex: 1, minWidth: 0 }}>
          <div style={{
            width: '40px',
            height: '40px',
            borderRadius: '10px',
            backgroundColor: theme.colors.bgCard,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: isConnected ? theme.colors.statusGreen : theme.colors.textMuted,
          }}>
            <Icons.Calendar />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 500 }}>
              Google Calendar
            </div>
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              marginTop: '2px',
            }}>
              {isConnected ? (
                <>
                  <div style={{
                    width: '6px',
                    height: '6px',
                    borderRadius: '50%',
                    backgroundColor: theme.colors.statusGreen,
                    flexShrink: 0,
                  }} />
                  <span style={{
                    color: theme.colors.statusGreen,
                    fontSize: '13px',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}>
                    {accountEmail ? `Connected - ${accountEmail}` : 'Connected'}
                  </span>
                </>
              ) : (
                <span style={{ color: theme.colors.textMuted, fontSize: '13px' }}>
                  Not connected
                </span>
              )}
            </div>
          </div>
        </div>

        {isConnected ? (
          <button
            onClick={() => setConfirmDisconnect('google_calendar')}
            disabled={disconnecting}
            onMouseEnter={() => setActionHovered(true)}
            onMouseLeave={() => setActionHovered(false)}
            style={{
              minHeight: '44px',
              boxSizing: 'border-box',
              padding: '8px 16px',
              borderRadius: '8px',
              border: `1px solid ${actionHovered ? theme.colors.statusRed + '60' : theme.colors.borderLight}`,
              backgroundColor: actionHovered ? theme.colors.statusRed + '15' : 'transparent',
              color: actionHovered ? theme.colors.statusRed : theme.colors.textSecondary,
              fontSize: '13px',
              fontWeight: 500,
              cursor: disconnecting ? 'not-allowed' : 'pointer',
              transition: 'all 0.15s ease',
              opacity: disconnecting ? 0.5 : 1,
              flexShrink: 0,
            }}
          >
            {disconnecting ? 'Disconnecting...' : 'Disconnect'}
          </button>
        ) : (
          <button
            onClick={handleConnect}
            disabled={!!connecting}
            onMouseEnter={() => setActionHovered(true)}
            onMouseLeave={() => setActionHovered(false)}
            style={{
              minHeight: '44px',
              boxSizing: 'border-box',
              padding: '8px 16px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: actionHovered ? theme.colors.accent : theme.colors.accent + 'E6',
              color: '#fff',
              fontSize: '13px',
              fontWeight: 600,
              cursor: connecting ? 'not-allowed' : 'pointer',
              transition: 'all 0.15s ease',
              opacity: connecting ? 0.5 : 1,
              flexShrink: 0,
            }}
          >
            {connecting ? 'Connecting...' : 'Connect'}
          </button>
        )}
      </div>

      {error && (
        <div style={{
          padding: '8px 20px 12px',
          color: theme.colors.statusRed,
          fontSize: '13px',
        }}>
          {error}
        </div>
      )}

      {/* Disconnect confirmation dialog */}
      {confirmDisconnect && (
        <div style={{
          margin: '0 20px 16px',
          padding: '16px',
          borderRadius: '12px',
          backgroundColor: theme.colors.bgCard,
          border: `1px solid ${theme.colors.borderLight}`,
        }}>
          <div style={{
            color: theme.colors.textPrimary,
            fontSize: '14px',
            fontWeight: 500,
            marginBottom: '8px',
          }}>
            Disconnect Google Calendar?
          </div>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '13px',
            marginBottom: '16px',
            lineHeight: '1.4',
          }}>
            You can reconnect anytime. Your calendar events will no longer appear in Viola.
          </div>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button
              onClick={() => setConfirmDisconnect(null)}
              style={{
                minHeight: '44px',
                boxSizing: 'border-box',
                padding: '8px 16px',
                borderRadius: '8px',
                border: `1px solid ${theme.colors.borderLight}`,
                backgroundColor: 'transparent',
                color: theme.colors.textSecondary,
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
                transition: 'all 0.15s ease',
              }}
            >
              Cancel
            </button>
            <button
              onClick={handleDisconnectConfirm}
              disabled={disconnecting}
              style={{
                minHeight: '44px',
                boxSizing: 'border-box',
                padding: '8px 16px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: theme.colors.statusRed,
                color: '#fff',
                fontSize: '13px',
                fontWeight: 600,
                cursor: disconnecting ? 'not-allowed' : 'pointer',
                transition: 'all 0.15s ease',
                opacity: disconnecting ? 0.7 : 1,
              }}
            >
              {disconnecting ? 'Disconnecting...' : 'Disconnect'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

const PhoneSettings = ({
  settings,
  onSettingChange,
  onDeleteAllCallData,
  isDeletingCallData,
  callDataDeleteStatus,
}) => (
  <>
    <SettingRow
      title="Your phone number"
      description="Used when Viola needs to call or text you for confirmations. This is not a Viola-owned public number."
    >
      <input
        type="tel"
        value={settings.user_phone_number || ''}
        onChange={(e) => onSettingChange('user_phone_number', e.target.value)}
        placeholder="+1 555 010 0000"
        style={{
          width: '180px',
          minHeight: '44px',
          padding: '10px 12px',
          borderRadius: '8px',
          border: `1px solid ${theme.colors.borderLight}`,
          backgroundColor: theme.colors.bgCard,
          color: theme.colors.textPrimary,
          fontSize: '13px',
          outline: 'none',
          boxSizing: 'border-box',
        }}
      />
    </SettingRow>
    <SectionDivider />
    <SettingRow
      title="Recipient callback number"
      description="Optional number Viola may give to businesses or voicemail systems when they need to return the call."
    >
      <input
        type="tel"
        value={settings.callback_phone || ''}
        onChange={(e) => onSettingChange('callback_phone', e.target.value)}
        placeholder="+1 555 010 0000"
        style={{
          width: '180px',
          minHeight: '44px',
          padding: '10px 12px',
          borderRadius: '8px',
          border: `1px solid ${theme.colors.borderLight}`,
          backgroundColor: theme.colors.bgCard,
          color: theme.colors.textPrimary,
          fontSize: '13px',
          outline: 'none',
          boxSizing: 'border-box',
        }}
      />
    </SettingRow>
    <SectionDivider />
    <SettingRow
      title="Record Phone Calls"
      description="Save raw call audio locally after the opening disclosure for your audit trail. Auto-deletes after 30 days."
    >
      <Toggle
        checked={settings.record_phone_calls ?? true}
        onChange={(v) => onSettingChange('record_phone_calls', v)}
        ariaLabel="Record phone calls"
      />
    </SettingRow>
    <SectionDivider />
    <SettingRow
      title="Keep Phone Transcript"
      description="Save call transcripts for your audit trail. Off means real-time listening only, with no transcript disclosure or persistence."
    >
      <Toggle
        checked={settings.keep_phone_transcript ?? true}
        onChange={(v) => onSettingChange('keep_phone_transcript', v)}
        ariaLabel="Keep phone transcript"
      />
    </SettingRow>
    <SectionDivider />
    <SettingRow
      title="Announce AI On Calls"
      description="Start calls with 'Viola, your automated assistant.' Viola still answers truthfully if asked when this is off."
    >
      <Toggle
        checked={settings.announce_ai_on_calls ?? false}
        onChange={(v) => onSettingChange('announce_ai_on_calls', v)}
        ariaLabel="Announce AI on calls"
      />
    </SettingRow>
    <SectionDivider />
    <div style={{ padding: '16px 20px' }}>
      <div style={{ color: theme.colors.textPrimary, fontSize: '13px', fontWeight: 600 }}>
        Call Data Retention
      </div>
      <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '8px' }}>
        Saved phone recordings, retained transcripts, and call metadata are auto-deleted after 30 days.
      </div>
    </div>
    <SectionDivider />
    <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
      <DangerButton
        color={theme.colors.statusRed}
        onClick={onDeleteAllCallData}
        disabled={isDeletingCallData}
      >
        {isDeletingCallData ? 'Deleting...' : 'Delete all call data now'}
      </DangerButton>
      {callDataDeleteStatus && (
        <span
          data-testid="call-data-delete-status"
          style={{
            color: callDataDeleteStatus.startsWith('Could not') ? theme.colors.statusRed : theme.colors.textMuted,
            fontSize: '12px',
          }}
        >
          {callDataDeleteStatus}
        </span>
      )}
    </div>
  </>
);

PhoneSettings.propTypes = {
  settings: PropTypes.object.isRequired,
  onSettingChange: PropTypes.func.isRequired,
  onDeleteAllCallData: PropTypes.func.isRequired,
  isDeletingCallData: PropTypes.bool,
  callDataDeleteStatus: PropTypes.string,
};

/**
 * Main Account Tab Component
 */
export function AccountTab({
  settings = {},
  onSettingChange = () => {},
  onDeleteAllCallData = () => {},
  isDeletingCallData = false,
  callDataDeleteStatus = '',
}) {
  const { user, subscription, loading, isLoggedIn, logout, passwordRecovery, mfaPending, refreshUser } = useAuth();
  const { toasts, addToast, removeToast } = useToast();

  // Cloud surface (api.useviola.com/app) authenticates through TWO GoTrue
  // session stores: CloudAuthGate/auth/AuthProvider (gates entry to the
  // dashboard) and this hooks/useAuth (lib/auth_context, used by ProfileCard/
  // subscription/usage below) — see App.jsx `Dashboard()`. The front door
  // bridges its session into the second store (#3547), so they reconcile; but
  // that bridge does a real GET /auth/v1/user round trip, so `isLoggedIn`
  // above is still false for the first paint after sign-in. Treat either
  // session as "logged in" (issue #1067) rather than flashing the full
  // Google/Apple/email sign-in form at an already-authenticated user.
  const { status: cloudStatus, user: cloudUser, signOut: cloudSignOut } = useCloudAuth();
  const cloudIsLoggedIn = cloudStatus === 'signedIn';
  const effectiveIsLoggedIn = isLoggedIn || cloudIsLoggedIn;
  const effectiveUser = user || cloudUser;

  // Billing/plan display (issue #2741). `subscription` above is the source of
  // truth and IS populated for a cloud sign-in — the front door's bridge is
  // what delivers it, and AccountTab.cloudStoreReconciliation.test.jsx proves
  // that against the real providers. This derivation covers only the first
  // paint before that bridge resolves: without it a paying customer briefly
  // sees "Free Plan", an upgrade prompt for the plan they already bought, and
  // no "Manage Subscription" button (candidate C-400). It is a fallback, never
  // the fix — a plan that renders right here while `subscription` stays null
  // is still broken everywhere else that reads the store (SmartDisplay's
  // gating, useUsage, Cloud Sync). Derived from the cloud user's OWN GoTrue
  // app_metadata via decodePlanFromUser — the signed-in user's own per-account
  // plan data (GoTrue scopes app_metadata per user), never a shared value.
  const cloudPlan = !subscription && cloudUser ? decodePlanFromUser(cloudUser) : null;
  const effectiveSubscription = subscription || (cloudPlan && {
    status: cloudPlan.subscriptionStatus,
    planId: cloudPlan.planId,
    planFamily: cloudPlan.planFamily,
    hasPaidAccess: cloudPlan.hasPaidAccess,
    paymentProvider: cloudPlan.paymentProvider,
  });

  // Sign Out must clear whichever session(s) are actually live — a click that
  // only cleared the disconnected hooks/useAuth session would leave a
  // cloud-authenticated user still signed in after "signing out".
  const handleLogout = useCallback(async () => {
    await Promise.allSettled([
      isLoggedIn ? logout() : null,
      cloudIsLoggedIn ? cloudSignOut() : null,
    ].filter(Boolean));
  }, [isLoggedIn, logout, cloudIsLoggedIn, cloudSignOut]);

  if (loading) {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '60px 20px',
        color: theme.colors.textMuted,
      }}>
        Loading...
      </div>
    );
  }

  // `mfaPending` = a password sign-in landed an AAL1 GoTrue session that still
  // needs a TOTP step-up. Don't show the signed-in profile over a
  // half-authenticated session — fall through to AuthForm's 'mfa' prompt.
  if (effectiveIsLoggedIn && !passwordRecovery && !mfaPending) {
    return (
      <>
        <Section title="Account">
          <ProfileCard user={effectiveUser} subscription={effectiveSubscription} onLogout={handleLogout} addToast={addToast} refreshUser={refreshUser} />
        </Section>

        <Section title="Phone">
          <PhoneSettings
            settings={settings}
            onSettingChange={onSettingChange}
            onDeleteAllCallData={onDeleteAllCallData}
            isDeletingCallData={isDeletingCallData}
            callDataDeleteStatus={callDataDeleteStatus}
          />
        </Section>

        <Section title="Sync & Devices">
          <SyncStatus enabled={!!effectiveSubscription?.hasPaidAccess} signedIn={effectiveIsLoggedIn} />
        </Section>
        <ToastContainer toasts={toasts} onDismiss={removeToast} />
      </>
    );
  }

  return (
    <>
      <Section title="Account">
        <AuthForm />
      </Section>

      <Section title="Phone">
        <PhoneSettings
          settings={settings}
          onSettingChange={onSettingChange}
          onDeleteAllCallData={onDeleteAllCallData}
          isDeletingCallData={isDeletingCallData}
          callDataDeleteStatus={callDataDeleteStatus}
        />
      </Section>
    </>
  );
}

AccountTab.propTypes = {
  settings: PropTypes.object,
  onSettingChange: PropTypes.func,
  onDeleteAllCallData: PropTypes.func,
  isDeletingCallData: PropTypes.bool,
  callDataDeleteStatus: PropTypes.string,
};

export default React.memo(AccountTab);
