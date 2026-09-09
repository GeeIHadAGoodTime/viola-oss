import { useState, useEffect, useCallback, useRef, memo } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { authFetch } from '../hooks/useViolaApi';
import { openSafeExternalUrl } from '../lib/safeUrl';
import { useCloudConsentGate } from '../hooks/cloudConsentGate';

const AUTO_DISMISS_BASE_MS = 12000;
const AUTO_DISMISS_PER_CHAR_MS = 40;
const AUTO_DISMISS_MAX_MS = 90000;
const SLIDE_ANIMATION_MS = 350;

function getAutoDismissMs(data) {
  if (data?.dismiss_after_ms) return data.dismiss_after_ms;

  let charCount = 0;
  if (data?.body) charCount += data.body.length;
  if (data?.items) charCount += data.items.join(' ').length;
  if (data?.rows) charCount += data.rows.flat().join(' ').length;
  if (data?.title) charCount += data.title.length;
  if (data?.subtitle) charCount += data.subtitle.length;

  const duration = AUTO_DISMISS_BASE_MS + (charCount * AUTO_DISMISS_PER_CHAR_MS);
  return Math.min(duration, AUTO_DISMISS_MAX_MS);
}

// =========================================================================
// ContentCard — Temporary overlay card for displaying visual content
//
// Renders different layouts based on data.type:
//   - list:   Bulleted list (shopping list, todos, etc.)
//   - info:   Single large value display (weather temp, countdown, etc.)
//   - table:  Simple table with headers and rows (queue, calendar, etc.)
//   - detail: Long-form text content (Wikipedia, recipes, etc.)
//
// Auto-dismisses based on content length with a progress bar.
// Click anywhere to dismiss. Slides in from the bottom of the screen.
//
// Theme: dark bg (#1a1a1a), white text, bronze/gold (#C89B3C) accents.
// =========================================================================

// --- Link rendering helper ---

function shortenUrl(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.replace(/^www\./, '');
    const firstPath = u.pathname.split('/').filter(Boolean)[0];
    const short = firstPath ? `${host}/${firstPath}` : host;
    return short.length > 40 ? short.slice(0, 37) + '...' : short;
  } catch {
    return url.length > 40 ? url.slice(0, 37) + '...' : url;
  }
}

const ExternalLinkIcon = () => (
  <svg
    width="11"
    height="11"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ marginLeft: '3px', verticalAlign: 'middle', opacity: 0.7 }}
  >
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <polyline points="15 3 21 3 21 9" />
    <line x1="10" y1="14" x2="21" y2="3" />
  </svg>
);

const URL_REGEX = /https?:\/\/[^\s)]+/g;

function renderTextWithLinks(text) {
  if (typeof text !== 'string') return text;

  const parts = [];
  let lastIndex = 0;
  let match;

  URL_REGEX.lastIndex = 0;
  while ((match = URL_REGEX.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const url = match[0];
    parts.push(
      <a
        key={`link-${match.index}`}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        onMouseOver={(e) => { e.currentTarget.style.textDecoration = 'underline'; }}
        onMouseOut={(e) => { e.currentTarget.style.textDecoration = 'none'; }}
        style={{
          color: THEME.colors.accent,
          textDecoration: 'none',
          transition: 'text-decoration 0.15s ease',
        }}
      >
        {shortenUrl(url)}
        <ExternalLinkIcon />
      </a>
    );
    lastIndex = match.index + url.length;
  }

  if (lastIndex === 0) return text;
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return parts;
}

// --- Sub-renderers for each content type ---

const ListContent = ({ data }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
    {data.items && data.items.map((item, i) => (
      <div
        key={i}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '12px',
          padding: '8px 0',
          borderBottom: i < data.items.length - 1
            ? `1px solid ${THEME.colors.borderSubtle}`
            : 'none',
        }}
      >
        <span style={{
          color: THEME.colors.accent,
          fontSize: '8px',
          lineHeight: '22px',
          flexShrink: 0,
        }}>
          {'\u25CF'}
        </span>
        <span style={{
          fontSize: 'clamp(14px, 1.6vw, 16px)',
          color: THEME.colors.textPrimary,
          lineHeight: '22px',
        }}>
          {renderTextWithLinks(item)}
        </span>
      </div>
    ))}
  </div>
);

ListContent.propTypes = {
  data: PropTypes.shape({
    items: PropTypes.arrayOf(PropTypes.string),
  }).isRequired,
};

const InfoContent = ({ data }) => (
  <div style={{
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '20px 0',
    gap: '6px',
  }}>
    <div style={{
      fontSize: 'clamp(52px, 10vw, 80px)',
      fontWeight: 200,
      lineHeight: 1.0,
      color: THEME.colors.accent,
      letterSpacing: '-3px',
      textShadow: `0 0 40px ${THEME.colors.accentGlow}`,
    }}>
      {data.value}
      {data.unit && (
        <span style={{
          fontSize: 'clamp(20px, 3vw, 28px)',
          fontWeight: 300,
          letterSpacing: '0px',
          marginLeft: '4px',
          color: THEME.colors.accentMuted || THEME.colors.accent,
          opacity: 0.7,
        }}>
          {data.unit}
        </span>
      )}
    </div>
  </div>
);

InfoContent.propTypes = {
  data: PropTypes.shape({
    value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
    unit: PropTypes.string,
  }).isRequired,
};

const TableContent = ({ data }) => (
  <div style={{
    overflowX: 'auto',
    WebkitOverflowScrolling: 'touch',
  }}>
    <table style={{
      width: '100%',
      borderCollapse: 'collapse',
      fontSize: 'clamp(14px, 1.5vw, 16px)',
    }}>
      {data.columns && (
        <thead>
          <tr>
            {data.columns.map((col, i) => (
              <th
                key={i}
                style={{
                  textAlign: 'left',
                  padding: '8px 12px',
                  color: THEME.colors.accent,
                  fontWeight: 500,
                  fontSize: 'clamp(11px, 1.2vw, 13px)',
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                  borderBottom: `1px solid ${THEME.colors.borderLight}`,
                  whiteSpace: 'nowrap',
                }}
              >
                {col}
              </th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>
        {data.rows && data.rows.map((row, rowIdx) => (
          <tr
            key={rowIdx}
            style={{
              borderBottom: rowIdx < data.rows.length - 1
                ? `1px solid ${THEME.colors.borderSubtle}`
                : 'none',
            }}
          >
            {row.map((cell, cellIdx) => (
              <td
                key={cellIdx}
                style={{
                  padding: '10px 12px',
                  color: cellIdx === 0
                    ? THEME.colors.textMuted
                    : THEME.colors.textPrimary,
                  fontWeight: 400,
                  whiteSpace: cellIdx === 0 ? 'nowrap' : 'normal',
                }}
              >
                {renderTextWithLinks(cell)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

TableContent.propTypes = {
  data: PropTypes.shape({
    columns: PropTypes.arrayOf(PropTypes.string),
    rows: PropTypes.arrayOf(PropTypes.arrayOf(PropTypes.string)),
  }).isRequired,
};

const DetailContent = ({ data }) => (
  <div style={{
    display: 'flex',
    flexDirection: 'column',
    gap: '12px',
  }}>
    {data.body && (
      <div style={{
        fontSize: 'clamp(15px, 1.6vw, 17px)',
        color: THEME.colors.textSecondary,
        lineHeight: 1.7,
        whiteSpace: 'pre-wrap',
      }}>
        {renderTextWithLinks(data.body)}
      </div>
    )}
    {data.source && (
      <div style={{
        fontSize: 'clamp(11px, 1.2vw, 13px)',
        color: THEME.colors.textMuted,
        fontStyle: 'italic',
        paddingTop: '4px',
        borderTop: `1px solid ${THEME.colors.borderSubtle}`,
      }}>
        Source: {data.source}
      </div>
    )}
  </div>
);

DetailContent.propTypes = {
  data: PropTypes.shape({
    body: PropTypes.string,
    source: PropTypes.string,
  }).isRequired,
};

// --- Gate review card (signature/payment confirmation) ---
//
// Rendered when the agent stops at a SIGNATURE_GATE or PAYMENT_GATE
// handoff. Payment gates link to Viola's hosted confirmation page;
// signature gates resume through the existing chat confirmation path.

const GateReviewContent = ({ data }) => {
  const [busy, setBusy] = useState(false);
  const interceptCloudConsent = useCloudConsentGate();

  const handleAction = useCallback(async (cta) => {
    if (!cta || busy) return;
    if (cta.action === 'open_url' && cta.url) {
      // cta.url is agent-controlled; the scheme guard drops javascript:/data:
      // payloads before they reach the navigation sink (SEC-018).
      openSafeExternalUrl(cta.url);
      return;
    }
    if (cta.action === 'send_chat' && cta.text) {
      // A CTA that sends a chat message IS a turn entry point, so it takes the
      // one shared first-run gate like every other one (#362). Outside the
      // cloud surface, and for an already-consented user, this never intercepts.
      if (interceptCloudConsent({ kind: 'text', text: cta.text })) return;
      setBusy(true);
      try {
        await authFetch('/v1/command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ text: cta.text, history: [], channel: data.origin_channel || 'web' }),
        });
      } catch { /* surface stays — user can retry from chat */ }
      setBusy(false);
    }
  }, [busy, data.origin_channel, interceptCloudConsent]);

  const ctaButtonStyle = (variant) => {
    const isPrimary = variant === 'primary';
    const isDanger = variant === 'danger';
    return {
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '10px 22px',
      fontSize: 'clamp(14px, 1.5vw, 16px)',
      fontWeight: 600,
      letterSpacing: '0.2px',
      borderRadius: '10px',
      cursor: busy ? 'wait' : 'pointer',
      opacity: busy ? 0.6 : 1,
      transition: 'transform 0.12s ease, background-color 0.12s ease',
      textDecoration: 'none',
      border: isPrimary ? 'none' : `1px solid ${isDanger ? '#a04040' : THEME.colors.borderLight}`,
      backgroundColor: isPrimary ? THEME.colors.accent : 'transparent',
      color: isPrimary ? '#fff' : (isDanger ? '#d97070' : THEME.colors.textPrimary),
    };
  };

  return (
    <div
      style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}
      onClick={(e) => e.stopPropagation()}
    >
      {data.body && (
        <div style={{
          fontSize: 'clamp(15px, 1.6vw, 17px)',
          color: THEME.colors.textSecondary,
          lineHeight: 1.5,
        }}>
          {renderTextWithLinks(data.body)}
        </div>
      )}

      {data.subject_url && (
        <div style={{
          fontSize: '13px',
          color: THEME.colors.textMuted,
          padding: '10px 14px',
          borderRadius: '8px',
          background: THEME.colors.bgElevated,
          border: `1px solid ${THEME.colors.borderSubtle}`,
          wordBreak: 'break-all',
        }}>
          <span style={{ color: THEME.colors.textDisabled, marginRight: '6px' }}>Page:</span>
          {renderTextWithLinks(data.subject_url)}
        </div>
      )}

      {Array.isArray(data.details) && data.details.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {data.details.map((d, i) => (
            <div key={i} style={{ fontSize: '14px', color: THEME.colors.textSecondary }}>
              <span style={{ color: THEME.colors.textMuted, marginRight: '8px' }}>{d.label}:</span>
              {renderTextWithLinks(d.value)}
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', justifyContent: 'flex-end', marginTop: '6px' }}>
        {data.tertiary_cta && (
          <button onClick={() => handleAction(data.tertiary_cta)} style={ctaButtonStyle('secondary')} disabled={busy}>
            {data.tertiary_cta.label}
          </button>
        )}
        {data.secondary_cta && (
          <button onClick={() => handleAction(data.secondary_cta)} style={ctaButtonStyle('danger')} disabled={busy}>
            {data.secondary_cta.label}
          </button>
        )}
        {data.cta && (
          <button onClick={() => handleAction(data.cta)} style={ctaButtonStyle('primary')} disabled={busy}>
            {data.cta.label}
          </button>
        )}
      </div>
    </div>
  );
};

GateReviewContent.propTypes = {
  data: PropTypes.shape({
    gate_kind: PropTypes.string,
    origin_channel: PropTypes.string,
    body: PropTypes.string,
    subject_url: PropTypes.string,
    details: PropTypes.arrayOf(PropTypes.shape({ label: PropTypes.string, value: PropTypes.string })),
    cta: PropTypes.object,
    secondary_cta: PropTypes.object,
    tertiary_cta: PropTypes.object,
  }).isRequired,
};

// --- Account-required card (Path A — direct cloud login proxy) ---
//
// Fires when a LOCAL_USER_ID / anonymous user tries a managed-LLM command.
// The desktop hub's `/auth/login` proxies email+password to api.useviola.com
// (see ASYNC-1 / Path A in DECISIONS_REGISTRY.md), so the user just enters
// their Viola account credentials in Settings → Account. No pair code.

// Must stay identical to core/account_gate.OPEN_ACCOUNT_SETTINGS_ACTION — the
// two halves of this door are in different languages with no compile-time link,
// so scripts/check_desktop_signin_door.py holds them together.
const OPEN_ACCOUNT_SETTINGS_ACTION = 'open_account_settings';

const AccountRequiredContent = ({ data }) => {
  const ctaButtonStyle = (isPrimary) => ({
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '10px 22px',
    fontSize: 'clamp(14px, 1.5vw, 16px)',
    fontWeight: 600,
    letterSpacing: '0.2px',
    borderRadius: '10px',
    cursor: 'pointer',
    transition: 'transform 0.12s ease, background-color 0.12s ease',
    textDecoration: 'none',
    border: isPrimary ? 'none' : `1px solid ${THEME.colors.borderLight}`,
    backgroundColor: isPrimary ? THEME.colors.accent : 'transparent',
    color: isPrimary ? '#fff' : THEME.colors.textPrimary,
  });

  const cta = data.cta || { label: 'Sign in', action: OPEN_ACCOUNT_SETTINGS_ACTION };
  const secondaryCta = data.secondary_cta;

  // This card is the wall a brand-new install hits on its very first managed
  // command, so where its button goes decides whether anyone ever gets past it.
  // It used to open useviola.com in the system browser — a dead end, because a
  // cloud account created on the website cannot become the desktop session this
  // gate is asking for. Sign-in has to happen HERE. Settings -> Account is where
  // the working form already lives, and it is exactly where the sibling
  // paid-action prompt has always sent people (SmartDisplay's LoginPromptModal
  // onSignIn). The URL is kept only as the fallback for a surface with no
  // in-app form of its own.
  const take = (target) => (e) => {
    e.stopPropagation();
    if (target?.action === OPEN_ACCOUNT_SETTINGS_ACTION) {
      window.dispatchEvent(new CustomEvent('viola:ui-action', {
        detail: { action: 'open_settings', payload: { tab: 'account' } },
      }));
      return;
    }
    // url is agent-controlled (data.cta.url); the scheme guard drops
    // javascript:/data: payloads before window.open / location.href (SEC-018).
    openSafeExternalUrl(target?.url);
  };

  return (
    <div
      style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}
      onClick={(e) => e.stopPropagation()}
    >
      {data.body && (
        <div style={{
          fontSize: 'clamp(15px, 1.6vw, 17px)',
          color: THEME.colors.textSecondary,
          lineHeight: 1.6,
        }}>
          {data.body}
        </div>
      )}
      <div style={{ display: 'flex', gap: '12px', justifyContent: 'center', flexWrap: 'wrap' }}>
        <button
          onClick={take(cta)}
          style={ctaButtonStyle(true)}
          onMouseOver={(e) => { e.currentTarget.style.transform = 'translateY(-1px)'; }}
          onMouseOut={(e) => { e.currentTarget.style.transform = 'translateY(0)'; }}
        >
          {cta.label || 'Sign in'}
        </button>
        {secondaryCta && (
          <button
            onClick={take(secondaryCta)}
            style={ctaButtonStyle(false)}
          >
            {secondaryCta.label || 'Create account'}
          </button>
        )}
      </div>
    </div>
  );
};

AccountRequiredContent.propTypes = {
  data: PropTypes.shape({
    body: PropTypes.string,
    cta: PropTypes.shape({
      label: PropTypes.string,
      action: PropTypes.string,
      url: PropTypes.string,
    }),
    secondary_cta: PropTypes.shape({
      label: PropTypes.string,
      action: PropTypes.string,
      url: PropTypes.string,
    }),
  }).isRequired,
};

// --- Main ContentCard component ---

function ContentCardInner({ data, onDismiss }) {
  const [visible, setVisible] = useState(false);
  const [exiting, setExiting] = useState(false);
  const [progress, setProgress] = useState(100);
  const timerRef = useRef(null);
  const animFrameRef = useRef(null);
  const startTimeRef = useRef(null);
  const exitingRef = useRef(false);
  const onDismissRef = useRef(onDismiss);

  useEffect(() => { onDismissRef.current = onDismiss; }, [onDismiss]);

  const dismissDuration = getAutoDismissMs(data);

  const handleDismiss = useCallback(() => {
    if (exitingRef.current) return;
    setExiting(true);
    exitingRef.current = true;
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    if (animFrameRef.current) { cancelAnimationFrame(animFrameRef.current); animFrameRef.current = null; }
    setTimeout(() => { onDismissRef.current(); }, SLIDE_ANIMATION_MS);
  }, []);

  // Inject scrollbar-hide style on mount
  useEffect(() => {
    const styleId = 'viola-content-card-scrollbar-hide';
    if (!document.getElementById(styleId)) {
      const style = document.createElement('style');
      style.id = styleId;
      style.textContent = '.viola-card-body::-webkit-scrollbar { display: none; }';
      document.head.appendChild(style);
    }
  }, []);

  // Slide in on mount
  useEffect(() => {
    const frameId = requestAnimationFrame(() => {
      setVisible(true);
    });
    return () => cancelAnimationFrame(frameId);
  }, []);

  // Auto-dismiss countdown with progress bar.
  // Account-required cards are exempt — the user is mid-signup flow and
  // racing them against a 90s timer would strand them on the website
  // while the card disappears on desktop. They can still dismiss via
  // the close button or Escape. See core/account_gate.py.
  const suppressAutoDismiss = data?.type === 'account_required' || data?.type === 'gate_review';

  useEffect(() => {
    if (suppressAutoDismiss) {
      setProgress(0);
      return undefined;
    }
    startTimeRef.current = Date.now();

    const tick = () => {
      const elapsed = Date.now() - startTimeRef.current;
      const remaining = Math.max(0, 100 - (elapsed / dismissDuration) * 100);
      setProgress(remaining);
      if (remaining > 0) {
        animFrameRef.current = requestAnimationFrame(tick);
      }
    };
    animFrameRef.current = requestAnimationFrame(tick);

    timerRef.current = setTimeout(handleDismiss, dismissDuration);

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    };
  }, [dismissDuration, handleDismiss, suppressAutoDismiss]);

  // Dismiss on Escape
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') handleDismiss();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [handleDismiss]);

  if (!data) return null;

  const cardType = data.type || 'info';

  // Render the type-specific content
  const renderContent = () => {
    switch (cardType) {
      case 'list':
        return <ListContent data={data} />;
      case 'info':
        return <InfoContent data={data} />;
      case 'table':
        return <TableContent data={data} />;
      case 'detail':
        return <DetailContent data={data} />;
      case 'account_required':
        return <AccountRequiredContent data={data} />;
      case 'gate_review':
        return <GateReviewContent data={data} />;
      default:
        return <DetailContent data={{ body: JSON.stringify(data, null, 2) }} />;
    }
  };

  return (
    <div
      onClick={handleDismiss}
      style={{
        position: 'fixed',
        bottom: 0,
        left: 0,
        right: 0,
        top: 0,
        zIndex: 900,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
        pointerEvents: 'auto',
        // Semi-transparent backdrop
        backgroundColor: exiting ? 'transparent' : 'rgba(0,0,0,0.45)',
        transition: `background-color ${SLIDE_ANIMATION_MS}ms ease`,
      }}
    >
      {/* The untitled fallback used to be "Content card" — this component's own
          name, read aloud to screen readers. Plain words instead. */}
      <div
        onClick={handleDismiss}
        role="dialog"
        aria-label={data.title || "Viola's response"}
        style={{
          width: '100%',
          maxWidth: 'min(720px, 92vw)',
          display: 'flex',
          flexDirection: 'column',
          backgroundColor: THEME.colors.bgElevated,
          borderRadius: '20px',
          border: `1px solid ${THEME.colors.borderLight}`,
          boxShadow: `0 20px 60px ${THEME.colors.shadowDeep}, 0 0 0 1px ${THEME.colors.borderLight}, 0 0 80px ${THEME.colors.accentGlow}`,
          overflow: 'hidden',
          cursor: 'pointer',
          // Slide-in / slide-out animation
          transform: exiting
            ? 'translateY(20px) scale(0.95)'
            : visible
              ? 'translateY(0) scale(1)'
              : 'translateY(20px) scale(0.95)',
          opacity: exiting ? 0 : visible ? 1 : 0,
          transition: `transform ${SLIDE_ANIMATION_MS}ms cubic-bezier(0.16, 1, 0.3, 1), opacity ${SLIDE_ANIMATION_MS}ms ease`,
        }}
      >
        {/* Header with gold accent line */}
        <div style={{
          padding: '24px 32px 16px 32px',
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: '12px',
          flexShrink: 0,
          borderBottom: `1px solid ${THEME.colors.borderSubtle}`,
        }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {data.title && (
              <div style={{
                fontSize: 'clamp(18px, 2.2vw, 22px)',
                fontWeight: 600,
                color: THEME.colors.accent,
                lineHeight: 1.3,
                letterSpacing: '0.3px',
              }}>
                {data.title}
              </div>
            )}
            {data.subtitle && (
              <div style={{
                fontSize: 'clamp(12px, 1.3vw, 14px)',
                color: THEME.colors.textMuted,
                marginTop: '4px',
              }}>
                {data.subtitle}
              </div>
            )}
          </div>
          {/* Close icon */}
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleDismiss();
            }}
            // Same class as the dialog's own aria-label above: "content card" is
            // this component's name, not something a user typed or asked for.
            aria-label="Dismiss"
            style={{
              background: 'none',
              border: 'none',
              color: THEME.colors.textDisabled,
              cursor: 'pointer',
              padding: '4px',
              display: 'flex',
              alignItems: 'center',
              flexShrink: 0,
              transition: 'color 0.15s ease',
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.color = THEME.colors.textSecondary;
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.color = THEME.colors.textDisabled;
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Content body — scrollable, hidden scrollbar */}
        <div
          className="viola-card-body"
          style={{
            padding: '20px 32px 28px 32px',
            overflowY: 'auto',
            scrollbarWidth: 'none',
            msOverflowStyle: 'none',
            flex: 1,
            minHeight: 0,
            maxHeight: '85vh',
          }}
        >
          {renderContent()}
        </div>

        {/* Progress bar for auto-dismiss countdown */}
        <div style={{
          height: '3px',
          backgroundColor: THEME.colors.borderSubtle,
          flexShrink: 0,
        }}>
          <div style={{
            height: '100%',
            width: `${progress}%`,
            backgroundColor: THEME.colors.accent,
            borderRadius: '0 2px 2px 0',
            transition: 'width 0.1s linear',
          }} />
        </div>
      </div>
    </div>
  );
}

ContentCardInner.propTypes = {
  data: PropTypes.shape({
    type: PropTypes.oneOf(['list', 'info', 'table', 'detail', 'account_required', 'gate_review']),
    title: PropTypes.string,
    subtitle: PropTypes.string,
    // list
    items: PropTypes.arrayOf(PropTypes.string),
    // info
    value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
    unit: PropTypes.string,
    // table
    columns: PropTypes.arrayOf(PropTypes.string),
    rows: PropTypes.arrayOf(PropTypes.arrayOf(PropTypes.string)),
    // detail
    body: PropTypes.string,
    source: PropTypes.string,
    // optional override
    dismiss_after_ms: PropTypes.number,
  }),
  onDismiss: PropTypes.func.isRequired,
};

const ContentCard = memo(ContentCardInner);
export default ContentCard;
