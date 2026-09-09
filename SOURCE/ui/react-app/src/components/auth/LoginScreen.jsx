/**
 * LoginScreen — Viola Cloud account sign-in.
 *
 * Consumes useAuth().signIn(email, password). On success the AuthProvider
 * flips `status` to 'signedIn' and the gate swaps in the dashboard, so this
 * screen does not navigate itself — it just reports the in-flight / error
 * states. Offers links to sign-up and password reset.
 *
 * Second factor (#2404): when the account has a verified TOTP factor, the
 * password sign-in only reaches AAL1 and `signIn` returns `mfaRequired` while
 * AuthProvider holds `mfaPending`. This screen then renders the 6-digit TOTP
 * prompt (driven off `mfaPending` so it survives a remount) and completes the
 * step-up through `verifyMfaTotp`. Until that succeeds the AAL1 session is
 * never treated as signed in, so an enrolled account cannot skip its code.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import AuthShell from './AuthShell';
import AuthField from './AuthField';
import AuthButton from './AuthButton';
import AuthNotice from './AuthNotice';
import { linkButtonStyle } from './authStyles';
import { validateEmail, validatePassword, authErrorMessage } from './authValidation';
import { THEME } from '../../config';
import { useAuth } from '../../auth/useAuth';

export default function LoginScreen({ onSwitchToSignUp, onForgotPassword, onResendVerification }) {
  const { signIn, verifyMfaTotp, cancelMfa, mfaPending } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [fieldErrors, setFieldErrors] = useState({});
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (busy) return;

    const emailError = validateEmail(email);
    const passwordError = validatePassword(password);
    if (emailError || passwordError) {
      setFieldErrors({ email: emailError, password: passwordError });
      return;
    }
    setFieldErrors({});
    setFormError('');
    setBusy(true);
    try {
      const result = await signIn(email.trim(), password);
      if (result?.ok) {
        // Signed in with no second factor — the gate re-renders the dashboard.
        return;
      }
      if (result?.mfaRequired) {
        // AAL1 accepted; a TOTP second factor is owed. The render flips to the
        // code prompt off mfaPending. Clear any prior error + stale code.
        setFormError('');
        setCode('');
        return;
      }
      setFormError(authErrorMessage(
        result?.error,
        "We couldn't sign you in. Check your email and password.",
      ));
    } catch {
      setFormError('Something went wrong. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  async function handleVerifyMfa(e) {
    e.preventDefault();
    if (busy) return;
    if (code.length !== 6) {
      setFormError('Enter the 6-digit code from your authenticator app.');
      return;
    }
    setFormError('');
    setBusy(true);
    try {
      const result = await verifyMfaTotp(code);
      if (!result?.ok) {
        setFormError(authErrorMessage(
          result?.error,
          'That code is not valid. Check your authenticator app and try again.',
        ));
        setCode('');
      }
      // On success the gate re-renders into the dashboard; nothing to do here.
    } catch {
      setFormError('Something went wrong. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  async function handleCancelMfa() {
    if (busy) return;
    setBusy(true);
    try {
      await cancelMfa();
    } finally {
      setPassword('');
      setCode('');
      setFormError('');
      setBusy(false);
    }
  }

  if (mfaPending) {
    return (
      <AuthShell
        title="Two-factor authentication"
        subtitle="Enter the 6-digit code from your authenticator app to finish signing in."
        footer={
          <span>
            <button type="button" style={linkButtonStyle()} onClick={handleCancelMfa} disabled={busy}>
              Back to sign in
            </button>
          </span>
        }
      >
        <form onSubmit={handleVerifyMfa} style={{ display: 'grid', gap: '16px' }} noValidate>
          <AuthNotice message={formError} kind="error" />
          <AuthField
            label="Authentication code"
            type="text"
            name="mfa-code"
            value={code}
            onChange={(v) => setCode(String(v).replace(/[^0-9]/g, '').slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="123456"
            disabled={busy}
            autoFocus
          />
          <AuthButton busy={busy}>{busy ? 'Verifying…' : 'Verify'}</AuthButton>
        </form>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Welcome back"
      subtitle="Sign in to your Viola account to pick up where you left off."
      footer={
        <span>
          New to Viola?{' '}
          <button type="button" style={linkButtonStyle()} onClick={onSwitchToSignUp}>
            Create an account
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
        <div style={{ display: 'grid', gap: '6px' }}>
          <AuthField
            label="Password"
            type="password"
            name="password"
            value={password}
            onChange={setPassword}
            autoComplete="current-password"
            placeholder="Your password"
            error={fieldErrors.password}
            disabled={busy}
          />
          <div style={{ justifySelf: 'end', display: 'flex', gap: '12px' }}>
            <button
              type="button"
              style={{ ...linkButtonStyle(), color: THEME.colors.textMuted, fontWeight: 500 }}
              onClick={() => onResendVerification(email.trim())}
            >
              Resend verification email?
            </button>
            <button
              type="button"
              style={{ ...linkButtonStyle(), color: THEME.colors.textMuted, fontWeight: 500 }}
              onClick={onForgotPassword}
            >
              Forgot password?
            </button>
          </div>
        </div>
        <AuthButton busy={busy}>{busy ? 'Signing in…' : 'Sign in'}</AuthButton>
      </form>
    </AuthShell>
  );
}

LoginScreen.propTypes = {
  onSwitchToSignUp: PropTypes.func.isRequired,
  onForgotPassword: PropTypes.func.isRequired,
  onResendVerification: PropTypes.func.isRequired,
};
