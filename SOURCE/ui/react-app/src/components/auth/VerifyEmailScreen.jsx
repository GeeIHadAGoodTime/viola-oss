/**
 * VerifyEmailScreen — resend a verification email, before OR after sign-up.
 *
 * Reached two ways (#2153): right after sign-up, with `email` already known
 * (the original flow — shows a static "we sent a link to X" message), or
 * self-service from the login screen's "Resend verification email?" link,
 * with no known email (a returning user who never verified and lost or
 * missed the original link — CL-20260716-6e27's affected-user shape). When
 * `email` isn't supplied, this screen collects it itself, mirroring
 * ResetPasswordScreen's editable-email pattern.
 *
 * Consumes useAuth().resendVerification(email) -> { ok, error }. The user
 * confirms by clicking the link in their inbox; once the AuthProvider sees a
 * verified session it flips `status` to 'signedIn' and the gate swaps in the
 * dashboard. This screen offers a resend action and a way back to sign-in.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import AuthShell from './AuthShell';
import AuthField from './AuthField';
import AuthButton from './AuthButton';
import AuthNotice from './AuthNotice';
import { linkButtonStyle } from './authStyles';
import { validateEmail, authErrorMessage } from './authValidation';
import { THEME } from '../../config';
import { useAuth } from '../../auth/useAuth';

function MailGlyph() {
  return (
    <div
      style={{
        width: '64px',
        height: '64px',
        borderRadius: '16px',
        justifySelf: 'center',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: THEME.colors.accentSubtle,
        border: `1px solid ${THEME.colors.accentBorder}`,
      }}
    >
      <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke={THEME.colors.accent} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="2" y="4" width="20" height="16" rx="2" />
        <path d="m22 7-10 6L2 7" />
      </svg>
    </div>
  );
}

export default function VerifyEmailScreen({ email: knownEmail, initialEmail = '', onBackToLogin }) {
  const { resendVerification } = useAuth();
  // A known email (post-signup) is shown as fixed text, matching the
  // original flow exactly. Arriving without one (the self-service entry
  // point) makes the field editable, pre-filled from `initialEmail` when the
  // caller has one (e.g. what the user had already typed into the login
  // form) so the user isn't asked to retype it.
  const [email, setEmail] = useState(knownEmail || initialEmail || '');
  const [fieldError, setFieldError] = useState('');
  const [busy, setBusy] = useState(false);
  const [resent, setResent] = useState(false);
  const [error, setError] = useState('');

  async function handleResend() {
    if (busy) return;
    const trimmed = email.trim();
    if (!knownEmail) {
      const emailError = validateEmail(trimmed);
      if (emailError) {
        setFieldError(emailError);
        return;
      }
    }
    setFieldError('');
    setBusy(true);
    setError('');
    setResent(false);
    try {
      const result = await resendVerification(trimmed);
      if (result?.ok) {
        setResent(true);
      } else {
        setError(authErrorMessage(
          result?.error,
          "We couldn't resend the email. Please try again.",
        ));
      }
    } catch {
      setError('Something went wrong. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell
      title="Verify your email"
      subtitle={
        knownEmail
          ? `We sent a verification link to ${knownEmail}. Open it to activate your account.`
          : "Enter your account email and we'll resend the verification link."
      }
      footer={
        <button type="button" style={linkButtonStyle()} onClick={onBackToLogin}>
          Back to sign in
        </button>
      }
    >
      <MailGlyph />
      {!knownEmail && (
        <AuthField
          label="Email"
          type="email"
          name="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          placeholder="you@example.com"
          error={fieldError}
          disabled={busy}
          autoFocus
        />
      )}
      <AuthNotice
        message={
          resent
            ? knownEmail
              ? 'Verification email sent — check your inbox.'
              : 'If an account exists with that email and needs verifying, a new link has been sent.'
            : ''
        }
        kind="success"
      />
      <AuthNotice message={error} kind="error" />
      <p style={{ margin: 0, fontSize: '13px', lineHeight: 1.55, color: THEME.colors.textSecondary, textAlign: 'center' }}>
        Didn&apos;t get it? Check your spam folder, or resend the link below.
        After verifying, sign in to reach your dashboard.
      </p>
      <div style={{ display: 'grid', gap: '10px' }}>
        <AuthButton type="button" busy={busy} onClick={handleResend}>
          {busy ? 'Resending…' : 'Resend verification email'}
        </AuthButton>
        <button
          type="button"
          style={{ ...linkButtonStyle(), color: THEME.colors.textMuted, fontWeight: 500, justifySelf: 'center' }}
          onClick={onBackToLogin}
        >
          I&apos;ve verified — go to sign in
        </button>
      </div>
    </AuthShell>
  );
}

VerifyEmailScreen.propTypes = {
  email: PropTypes.string,
  initialEmail: PropTypes.string,
  onBackToLogin: PropTypes.func.isRequired,
};
