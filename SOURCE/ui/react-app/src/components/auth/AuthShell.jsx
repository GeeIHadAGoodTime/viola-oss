/**
 * AuthShell — branded layout frame for every Viola Cloud auth screen.
 *
 * Renders the full-bleed dark page with the accent glow, the Viola wordmark,
 * and a glass card holding the screen's content. Keeps the four auth screens
 * (login, sign-up, reset, verify) visually consistent and on-brand.
 */
import React from 'react';
import PropTypes from 'prop-types';
import {
  pageStyle,
  cardStyle,
  headingStyle,
  subheadingStyle,
  FONT_STACK,
} from './authStyles';
import { THEME } from '../../config';

/** Viola wordmark + icon — sits above the card. */
function Wordmark() {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '11px',
        justifyContent: 'center',
      }}
    >
      <img
        src={`${import.meta.env.BASE_URL || '/'}viola_icon.png`}
        alt=""
        width="34"
        height="34"
        style={{ borderRadius: '8px' }}
      />
      <span
        style={{
          fontSize: '20px',
          fontWeight: 700,
          letterSpacing: '-0.3px',
          color: THEME.colors.textBright,
          fontFamily: FONT_STACK,
        }}
      >
        Viola
      </span>
    </div>
  );
}

export default function AuthShell({ title, subtitle, children, footer }) {
  return (
    <div style={pageStyle()} data-testid="auth-shell">
      <main
        style={{
          width: 'min(420px, 100%)',
          display: 'grid',
          gap: '20px',
          justifyItems: 'stretch',
        }}
      >
        <Wordmark />
        <section style={cardStyle()}>
          <header style={{ display: 'grid', gap: '7px', textAlign: 'center' }}>
            <h1 style={headingStyle()}>{title}</h1>
            {subtitle ? <p style={subheadingStyle()}>{subtitle}</p> : null}
          </header>
          {children}
        </section>
        {footer ? (
          <footer
            style={{
              textAlign: 'center',
              fontSize: '13px',
              color: THEME.colors.textMuted,
            }}
          >
            {footer}
          </footer>
        ) : null}
      </main>
    </div>
  );
}

AuthShell.propTypes = {
  title: PropTypes.string.isRequired,
  subtitle: PropTypes.node,
  children: PropTypes.node.isRequired,
  footer: PropTypes.node,
};
