/**
 * RecoveryConfirmScreen — the set-new-password step of the browser password
 * reset (#2608).
 *
 * The recovery email link runs through GoTrue's `/verify?type=recovery`, which
 * redirects back to the SPA (`/app`) with the recovery session in the URL
 * fragment (`#access_token=...&type=recovery`). CloudAuthGate detects that hash
 * and mounts this screen. Here the user picks a new password, which is set via
 * GoTrue `PUT /user` using the recovery-session access token (see
 * authClient.updatePasswordWithToken). On success the server revokes the
 * recovery session family, so we send the user back to sign in with the new
 * password.
 *
 * This lives in the front-door (signed-out) store on purpose: a recovering user
 * is NOT signed in, so AccountTab's in-dashboard reset-confirm can never fire
 * for them (it is behind the sign-in gate). This is the front-door counterpart.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import AuthShell from './AuthShell';
import AuthField from './AuthField';
import AuthButton from './AuthButton';
import AuthNotice from './AuthNotice';
import { linkButtonStyle } from './authStyles';
import { validateNewPassword, authErrorMessage } from './authValidation';
import { updatePasswordWithToken } from '../../auth/authClient';

export default function RecoveryConfirmScreen({ accessToken, onDone }) {
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [fieldError, setFieldError] = useState('');
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (busy) return;

    const pwError = validateNewPassword(password);
    if (pwError) {
      setFieldError(pwError);
      return;
    }
    if (password !== confirmPassword) {
      setFieldError('Passwords do not match.');
      return;
    }
    setFieldError('');
    setFormError('');

    if (!accessToken) {
      setFormError('This reset link is missing or expired. Request a new one.');
      return;
    }

    setBusy(true);
    try {
      const result = await updatePasswordWithToken(accessToken, password);
      if (result?.ok) {
        setDone(true);
      } else {
        setFormError(authErrorMessage(
          result?.error,
          'We could not update your password. The reset link may have expired — request a new one.',
        ));
      }
    } catch {
      setFormError('Something went wrong. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <AuthShell
        title="Password updated"
        subtitle="Sign in with your new password to continue."
        footer={
          <button type="button" style={linkButtonStyle()} onClick={onDone}>
            Back to sign in
          </button>
        }
      >
        <AuthNotice message="Your password was changed. For your security, we signed you out everywhere." kind="success" />
        <AuthButton type="button" onClick={onDone}>
          Sign in
        </AuthButton>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Set a new password"
      subtitle="Choose a new password for your Viola account."
      footer={
        <button type="button" style={linkButtonStyle()} onClick={onDone}>
          Back to sign in
        </button>
      }
    >
      <form onSubmit={handleSubmit} style={{ display: 'grid', gap: '16px' }} noValidate>
        <AuthNotice message={formError} kind="error" />
        <AuthField
          label="New password"
          type="password"
          name="new-password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          placeholder="At least 12 characters"
          error={fieldError}
          disabled={busy}
          autoFocus
        />
        <AuthField
          label="Confirm new password"
          type="password"
          name="confirm-password"
          value={confirmPassword}
          onChange={setConfirmPassword}
          autoComplete="new-password"
          placeholder="Re-enter your new password"
          disabled={busy}
        />
        <AuthButton busy={busy}>{busy ? 'Updating…' : 'Update password'}</AuthButton>
      </form>
    </AuthShell>
  );
}

RecoveryConfirmScreen.propTypes = {
  accessToken: PropTypes.string,
  onDone: PropTypes.func.isRequired,
};
