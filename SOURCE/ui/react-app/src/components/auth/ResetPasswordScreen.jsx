/**
 * ResetPasswordScreen — request a Viola Cloud password-reset email.
 *
 * Consumes useAuth().resetPassword(email) -> { ok, error }. On success the
 * screen shows a "check your inbox" confirmation rather than navigating —
 * the actual password change happens via the emailed link. The confirmation
 * copy is intentionally enumeration-safe: it never reveals whether the email
 * is registered.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import AuthShell from './AuthShell';
import AuthField from './AuthField';
import AuthButton from './AuthButton';
import AuthNotice from './AuthNotice';
import { linkButtonStyle } from './authStyles';
import { validateEmail, authErrorMessage } from './authValidation';
import { useAuth } from '../../auth/useAuth';

export default function ResetPasswordScreen({ onBackToLogin, initialEmail = '' }) {
  const { resetPassword } = useAuth();
  const [email, setEmail] = useState(initialEmail);
  const [fieldError, setFieldError] = useState('');
  const [formError, setFormError] = useState('');
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (busy) return;

    const emailError = validateEmail(email);
    if (emailError) {
      setFieldError(emailError);
      return;
    }
    setFieldError('');
    setFormError('');
    setBusy(true);
    try {
      const result = await resetPassword(email.trim());
      if (result?.ok) {
        setSent(true);
      } else {
        setFormError(authErrorMessage(
          result?.error,
          "We couldn't send the reset email. Please try again.",
        ));
      }
    } catch {
      setFormError('Something went wrong. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <AuthShell
        title="Check your inbox"
        subtitle={`If an account exists for ${email.trim()}, we've sent a link to reset your password.`}
        footer={
          <button type="button" style={linkButtonStyle()} onClick={onBackToLogin}>
            Back to sign in
          </button>
        }
      >
        <AuthNotice
          message="The reset link expires soon — open it from this device if you can."
          kind="success"
        />
        <AuthButton type="button" onClick={onBackToLogin}>
          Back to sign in
        </AuthButton>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Reset your password"
      subtitle="Enter your account email and we'll send you a link to set a new password."
      footer={
        <button type="button" style={linkButtonStyle()} onClick={onBackToLogin}>
          Back to sign in
        </button>
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
          error={fieldError}
          disabled={busy}
          autoFocus
        />
        <AuthButton busy={busy}>{busy ? 'Sending…' : 'Send reset link'}</AuthButton>
      </form>
    </AuthShell>
  );
}

ResetPasswordScreen.propTypes = {
  onBackToLogin: PropTypes.func.isRequired,
  initialEmail: PropTypes.string,
};
