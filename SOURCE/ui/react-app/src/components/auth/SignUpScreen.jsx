/**
 * SignUpScreen — Viola Cloud account creation.
 *
 * Consumes useAuth().signUp(email, password) -> { ok, needsEmailVerification,
 * error }. When the backend requires email verification, this screen hands
 * the email up to the parent so the gate can show VerifyEmailScreen. When the
 * account is usable immediately, the AuthProvider flips `status` to
 * 'signedIn' and the gate swaps in the dashboard.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import AuthShell from './AuthShell';
import AuthField from './AuthField';
import AuthButton from './AuthButton';
import AuthNotice from './AuthNotice';
import { linkButtonStyle } from './authStyles';
import {
  validateEmail,
  validateNewPassword,
  authErrorMessage,
  MIN_PASSWORD_LENGTH,
} from './authValidation';
import { THEME } from '../../config';
import { useAuth } from '../../auth/useAuth';
import { ACCEPTED_PRIVACY_VERSION, ACCEPTED_TERMS_VERSION } from '../../auth/legalVersions';

export default function SignUpScreen({ onSwitchToLogin, onNeedsVerification }) {
  const { signUp } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [eligibilityConfirmed, setEligibilityConfirmed] = useState(false);
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [fieldErrors, setFieldErrors] = useState({});
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (busy) return;

    const emailError = validateEmail(email);
    const passwordError = validateNewPassword(password);
    const confirmError = !confirm
      ? 'Re-enter your password.'
      : confirm !== password
        ? 'Passwords do not match.'
        : null;
    if (emailError || passwordError || confirmError) {
      setFieldErrors({ email: emailError, password: passwordError, confirm: confirmError });
      return;
    }
    if (!eligibilityConfirmed || !termsAccepted) {
      setFormError('Confirm eligibility and accept the Terms of Service to create an account.');
      return;
    }
    setFieldErrors({});
    setFormError('');
    setBusy(true);
    try {
      const result = await signUp(email.trim(), password, {
        tosAccepted: true,
        legalEligibilityConfirmed: true,
        termsVersion: ACCEPTED_TERMS_VERSION,
        privacyVersion: ACCEPTED_PRIVACY_VERSION,
      });
      if (!result?.ok) {
        setFormError(authErrorMessage(
          result?.error,
          "We couldn't create your account. Please try again.",
        ));
        return;
      }
      if (result.needsEmailVerification) {
        onNeedsVerification(email.trim());
        return;
      }
      // Account usable immediately — the AuthProvider flips status to
      // 'signedIn' and the gate swaps in the dashboard.
    } catch {
      setFormError('Something went wrong. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell
      title="Create your account"
      subtitle="Set up Viola Cloud — your assistant follows you across every device."
      footer={
        <span>
          Already have an account?{' '}
          <button type="button" style={linkButtonStyle()} onClick={onSwitchToLogin}>
            Sign in
          </button>
        </span>
      }
    >
      <form onSubmit={handleSubmit} style={{ display: 'grid', gap: '16px' }} noValidate>
        <AuthNotice message={formError} kind="error" />
        <AuthField
          label="Email"
          type="email"
          name="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          placeholder="you@example.com"
          error={fieldErrors.email}
          disabled={busy}
          autoFocus
        />
        <AuthField
          label="Password"
          type="password"
          name="new-password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
          error={fieldErrors.password}
          disabled={busy}
        />
        <AuthField
          label="Confirm password"
          type="password"
          name="confirm-password"
          value={confirm}
          onChange={setConfirm}
          autoComplete="new-password"
          placeholder="Re-enter your password"
          error={fieldErrors.confirm}
          disabled={busy}
        />
        <label style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', fontSize: '12px', lineHeight: 1.45, color: THEME.colors.textMuted, minHeight: '44px' }}>
          <input
            type="checkbox"
            checked={eligibilityConfirmed}
            onChange={(event) => setEligibilityConfirmed(event.target.checked)}
            disabled={busy}
            style={{ marginTop: '2px', accentColor: THEME.colors.accent }}
          />
          <span>I confirm that I meet the age and guardian-consent requirements in the Terms of Service.</span>
        </label>
        <label style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', fontSize: '12px', lineHeight: 1.45, color: THEME.colors.textMuted, minHeight: '44px' }}>
          <input
            type="checkbox"
            checked={termsAccepted}
            onChange={(event) => setTermsAccepted(event.target.checked)}
            disabled={busy}
            style={{ marginTop: '2px', accentColor: THEME.colors.accent }}
          />
          <span>
            I agree to Viola&apos;s{' '}
            <a href="https://useviola.com/terms" target="_blank" rel="noopener noreferrer" style={linkButtonStyle()}>
              Terms of Service
            </a>{' '}
            and{' '}
            <a href="https://useviola.com/privacy" target="_blank" rel="noopener noreferrer" style={linkButtonStyle()}>
              Privacy Policy
            </a>
            .
          </span>
        </label>
        <AuthButton busy={busy} disabled={!eligibilityConfirmed || !termsAccepted}>
          {busy ? 'Creating account…' : 'Create account'}
        </AuthButton>
      </form>
    </AuthShell>
  );
}

SignUpScreen.propTypes = {
  onSwitchToLogin: PropTypes.func.isRequired,
  onNeedsVerification: PropTypes.func.isRequired,
};
