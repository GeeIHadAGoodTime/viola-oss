/**
 * authStyles — shared visual tokens for the Viola Cloud auth front door.
 *
 * The auth screens are the product's first impression, so they read the
 * canonical THEME palette (config.js) at render time rather than hard-coding
 * colors. Everything here is a plain style object / helper so the screens
 * stay dependency-free and easy to unit test.
 */
import { THEME } from '../../config';

/** Brand display font stack — matches main.jsx's BrandedLoader. */
export const FONT_STACK =
  "'Segoe UI', 'SF Pro Display', -apple-system, BlinkMacSystemFont, sans-serif";

/**
 * Full-bleed page background. A near-black void with a soft accent-tinted
 * radial glow behind the card — Viola's "deep space + warm ember" language.
 */
export function pageStyle() {
  const c = THEME.colors;
  return {
    position: 'fixed',
    inset: 0,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '24px',
    overflowY: 'auto',
    fontFamily: FONT_STACK,
    color: c.textPrimary,
    backgroundColor: c.bgVoid,
    backgroundImage: `radial-gradient(circle at 50% 28%, ${c.accentGlow} 0%, transparent 55%)`,
  };
}

/**
 * The glass card that holds a single auth screen.
 */
export function cardStyle() {
  const c = THEME.colors;
  return {
    width: 'min(420px, 100%)',
    display: 'grid',
    gap: '22px',
    padding: '34px 32px 30px',
    borderRadius: '20px',
    border: `1px solid ${c.borderLight}`,
    backgroundColor: c.bgCard,
    boxShadow: `0 24px 64px ${c.shadowDeep}, 0 0 0 1px ${c.borderSubtle}`,
    backdropFilter: 'blur(18px)',
    WebkitBackdropFilter: 'blur(18px)',
  };
}

export function headingStyle() {
  return {
    margin: 0,
    fontSize: '23px',
    fontWeight: 650,
    letterSpacing: '-0.4px',
    color: THEME.colors.textBright,
  };
}

export function subheadingStyle() {
  return {
    margin: 0,
    fontSize: '14px',
    lineHeight: 1.55,
    color: THEME.colors.textSecondary,
  };
}

export function labelStyle() {
  return {
    fontSize: '12.5px',
    fontWeight: 600,
    letterSpacing: '0.2px',
    color: THEME.colors.textSecondary,
  };
}

export function inputStyle(invalid = false) {
  const c = THEME.colors;
  return {
    width: '100%',
    minHeight: '44px',
    padding: '0 14px',
    fontSize: '14.5px',
    fontFamily: FONT_STACK,
    color: c.textBright,
    backgroundColor: c.bgSurface,
    border: `1px solid ${invalid ? c.statusRed : c.borderLight}`,
    borderRadius: '11px',
    outline: 'none',
    boxSizing: 'border-box',
    transition: 'border-color 0.15s ease, box-shadow 0.15s ease',
  };
}

export function inputFocusBoxShadow() {
  return `0 0 0 3px ${THEME.colors.accentRing}`;
}

/**
 * Primary call-to-action button (Sign in / Create account).
 */
export function primaryButtonStyle(disabled = false) {
  const c = THEME.colors;
  return {
    width: '100%',
    minHeight: '46px',
    padding: '0 18px',
    fontSize: '15px',
    fontWeight: 650,
    fontFamily: FONT_STACK,
    color: c.textBright,
    border: `1px solid ${c.accentBorder}`,
    borderRadius: '12px',
    backgroundColor: c.accent,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.55 : 1,
    transition: 'filter 0.15s ease, opacity 0.15s ease',
  };
}

/**
 * Quiet text link button used for "switch to sign up", "forgot password", etc.
 *
 * Renders inline in a sentence ("New to Viola? Create an account"), so it
 * can't just grow to a 44px box the way a standalone button can -- that would
 * shove the surrounding line apart. Vertical padding on an inline element
 * doesn't affect line-box layout at all (CSS inline formatting model), so it
 * grows the actual click/tap target to the 44px mobile touch-target floor
 * (issue #367) for free; the matching negative horizontal margin cancels out
 * the padding's width growth so the visible text still sits flush against
 * its neighbors.
 */
export function linkButtonStyle() {
  return {
    background: 'none',
    border: 'none',
    padding: '15px 4px',
    margin: '0 -4px',
    fontSize: '13.5px',
    fontWeight: 600,
    fontFamily: FONT_STACK,
    color: THEME.colors.accent,
    cursor: 'pointer',
    textDecoration: 'none',
  };
}

/**
 * Inline error / notice banner.
 */
export function noticeStyle(kind = 'error') {
  const c = THEME.colors;
  const accent = kind === 'success' ? c.statusGreen : c.statusRed;
  return {
    display: 'flex',
    gap: '9px',
    alignItems: 'flex-start',
    padding: '11px 13px',
    fontSize: '13px',
    lineHeight: 1.5,
    color: c.textBright,
    borderRadius: '10px',
    border: `1px solid ${accent}`,
    backgroundColor: kind === 'success'
      ? 'rgba(34, 197, 94, 0.10)'
      : 'rgba(239, 68, 68, 0.10)',
  };
}
