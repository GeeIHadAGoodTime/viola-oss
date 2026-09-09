/**
 * Tests for the Viola Cloud auth front door.
 *
 * Covers the surface detection, the gate's status-driven rendering, screen
 * navigation, and each screen's submit/validation/error behaviour. The
 * `useAuth()` hook is mocked so these tests do not depend on the GoTrue
 * authClient (owned + still being completed by a parallel agent).
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ---- Mock the parallel-agent auth hook ------------------------------------
// Every screen imports `useAuth` from '../../auth/useAuth'. We mock that
// module so tests drive the auth actions directly.
const mockAuth = {
  status: 'signedOut',
  user: null,
  session: null,
  mfaPending: false,
  mfaFactorId: null,
  signIn: vi.fn(),
  verifyMfaTotp: vi.fn(),
  cancelMfa: vi.fn(),
  signUp: vi.fn(),
  signOut: vi.fn(),
  resetPassword: vi.fn(),
  resendVerification: vi.fn(),
};

vi.mock('../../auth/useAuth', () => ({
  useAuth: () => mockAuth,
  default: () => mockAuth,
}));

import { isCloudSurface, isSpokeRoute } from './cloudSurface';
import { authErrorMessage, validateEmail, validateNewPassword } from './authValidation';
import { ACCEPTED_PRIVACY_VERSION, ACCEPTED_TERMS_VERSION } from '../../auth/legalVersions';
import CloudAuthGate from './CloudAuthGate';
import LoginScreen from './LoginScreen';
import SignUpScreen from './SignUpScreen';
import ResetPasswordScreen from './ResetPasswordScreen';
import RecoveryConfirmScreen from './RecoveryConfirmScreen';
import VerifyEmailScreen from './VerifyEmailScreen';

function resetAuthMock() {
  mockAuth.status = 'signedOut';
  mockAuth.user = null;
  mockAuth.session = null;
  mockAuth.mfaPending = false;
  mockAuth.mfaFactorId = null;
  mockAuth.verifyMfaTotp = vi.fn(async () => ({ ok: true, error: null }));
  mockAuth.cancelMfa = vi.fn(async () => ({ ok: true, error: null }));
  mockAuth.signIn = vi.fn(async () => ({ ok: true, error: null }));
  mockAuth.signUp = vi.fn(async () => ({ ok: true, needsEmailVerification: true, error: null }));
  mockAuth.signOut = vi.fn(async () => ({ ok: true, error: null }));
  mockAuth.resetPassword = vi.fn(async () => ({ ok: true, error: null }));
  mockAuth.resendVerification = vi.fn(async () => ({ ok: true, error: null }));
}

beforeEach(() => {
  resetAuthMock();
  window.history.replaceState({}, '', '/');
  delete window.viola;
});

afterEach(() => {
  cleanup();
  delete window.viola;
});

// ===========================================================================
// Surface detection
// ===========================================================================
describe('cloudSurface', () => {
  it('treats a plain browser at the SPA root as the cloud surface', () => {
    expect(isCloudSurface()).toBe(true);
  });

  it('is NOT the cloud surface when the Qt desktop bridge is present', () => {
    window.viola = {};
    expect(isCloudSurface()).toBe(false);
  });

  it('is NOT the cloud surface for a multiroom spoke route', () => {
    window.history.replaceState({}, '', '/?room=kitchen&spoke_token=qr-token');
    expect(isSpokeRoute()).toBe(true);
    expect(isCloudSurface()).toBe(false);
  });

  it('is NOT the cloud surface for the review route', () => {
    window.history.replaceState({}, '', '/?mode=review');
    expect(isCloudSurface()).toBe(false);
  });
});

// ===========================================================================
// authValidation helpers
// ===========================================================================
describe('authValidation', () => {
  it('rejects malformed emails and accepts well-formed ones', () => {
    expect(validateEmail('')).toBeTruthy();
    expect(validateEmail('notanemail')).toBeTruthy();
    expect(validateEmail('user@example.com')).toBeNull();
  });

  it('enforces a minimum new-password length', () => {
    expect(validateNewPassword('short')).toBeTruthy();
    expect(validateNewPassword('longenough123')).toBeNull();
  });

  it('extracts the message from useAuth() object-shaped errors', () => {
    expect(authErrorMessage({ message: 'Bad creds', code: 'invalid' }, 'fb')).toBe('Bad creds');
    expect(authErrorMessage(null, 'fallback')).toBe('fallback');
    expect(authErrorMessage('raw string', 'fb')).toBe('raw string');
    expect(authErrorMessage({ code: 'x' }, 'fb')).toBe('fb');
  });
});

// ===========================================================================
// CloudAuthGate — status-driven rendering
// ===========================================================================
describe('CloudAuthGate', () => {
  it('shows the branded loading screen while auth status is loading', () => {
    mockAuth.status = 'loading';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);
    expect(screen.getByTestId('auth-loading')).toBeInTheDocument();
    expect(screen.queryByText('dashboard')).not.toBeInTheDocument();
  });

  it('renders the login screen when signed out', () => {
    mockAuth.status = 'signedOut';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);
    expect(screen.getByRole('heading', { name: /Welcome back/i })).toBeInTheDocument();
    expect(screen.queryByText('dashboard')).not.toBeInTheDocument();
  });

  it('renders the dashboard children once signed in', () => {
    mockAuth.status = 'signedIn';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);
    expect(screen.getByText('dashboard')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Welcome back/i })).not.toBeInTheDocument();
  });

  it('navigates login -> sign up -> back to login', async () => {
    const user = userEvent.setup();
    mockAuth.status = 'signedOut';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);

    await user.click(screen.getByRole('button', { name: /Create an account/i }));
    expect(screen.getByRole('heading', { name: /Create your account/i })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Sign in$/i }));
    expect(screen.getByRole('heading', { name: /Welcome back/i })).toBeInTheDocument();
  });

  it('navigates login -> reset password', async () => {
    const user = userEvent.setup();
    mockAuth.status = 'signedOut';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);

    await user.click(screen.getByRole('button', { name: /Forgot password/i }));
    expect(screen.getByRole('heading', { name: /Reset your password/i })).toBeInTheDocument();
  });

  it('routes sign up -> verify email when verification is required', async () => {
    const user = userEvent.setup();
    mockAuth.status = 'signedOut';
    mockAuth.signUp = vi.fn(async () => ({ ok: true, needsEmailVerification: true, error: null }));
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);

    await user.click(screen.getByRole('button', { name: /Create an account/i }));
    await user.type(screen.getByLabelText(/^Email$/i), 'new@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'longenough123');
    await user.type(screen.getByLabelText(/Confirm password/i), 'longenough123');
    await user.click(screen.getByLabelText(/age and guardian-consent requirements/i));
    await user.click(screen.getByLabelText(/I agree to Viola's/i));
    await user.click(screen.getByRole('button', { name: /Create account/i }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Verify your email/i })).toBeInTheDocument();
    });
    expect(screen.getByText(/new@example.com/)).toBeInTheDocument();
  });

  it('shows the set-new-password screen when a recovery link lands on the SPA (#2608)', () => {
    // A recovering user is signed out; the recovery session arrives in the URL
    // fragment. CloudAuthGate must show the confirm screen, not the login form.
    mockAuth.status = 'signedOut';
    window.location.hash = '#access_token=recovery-jwt&type=recovery&expires_in=300';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);
    expect(screen.getByRole('heading', { name: /Set a new password/i })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Welcome back/i })).not.toBeInTheDocument();
    window.location.hash = '';
  });

  it('ignores a non-recovery hash and renders the login screen', () => {
    mockAuth.status = 'signedOut';
    window.location.hash = '#access_token=abc&type=magiclink';
    render(<CloudAuthGate><div>dashboard</div></CloudAuthGate>);
    expect(screen.getByRole('heading', { name: /Welcome back/i })).toBeInTheDocument();
    window.location.hash = '';
  });
});

// ===========================================================================
// RecoveryConfirmScreen — the set-new-password step (#2608)
// ===========================================================================
describe('RecoveryConfirmScreen', () => {
  function mockFetchResponse({ status = 200, body = {} } = {}) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => null },
      text: () => Promise.resolve(status === 204 ? '' : JSON.stringify(body)),
    };
  }

  it('sets a new password via GoTrue PUT /user with the recovery token, then confirms', async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockResolvedValue(mockFetchResponse({ body: { id: 'user-1' } }));
    const onDone = vi.fn();
    render(<RecoveryConfirmScreen accessToken="recovery-jwt" onDone={onDone} />);

    await user.type(screen.getByLabelText(/^New password$/i), 'BrandNewPassword42!');
    await user.type(screen.getByLabelText(/Confirm new password/i), 'BrandNewPassword42!');
    await user.click(screen.getByRole('button', { name: /Update password/i }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Password updated/i })).toBeInTheDocument();
    });
    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).pathname).toBe('/auth/v1/user');
    expect(init.method).toBe('PUT');
    expect(init.headers.Authorization).toBe('Bearer recovery-jwt');
    expect(JSON.parse(init.body)).toEqual({ password: 'BrandNewPassword42!' }); // pragma: allowlist secret
  });

  it('blocks submission when the two passwords do not match', async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn();
    render(<RecoveryConfirmScreen accessToken="recovery-jwt" onDone={vi.fn()} />);

    await user.type(screen.getByLabelText(/^New password$/i), 'BrandNewPassword42!');
    await user.type(screen.getByLabelText(/Confirm new password/i), 'Mismatchxxxxx!');
    await user.click(screen.getByRole('button', { name: /Update password/i }));

    expect(screen.getByText(/do not match/i)).toBeInTheDocument();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('surfaces a server rejection (expired link) without claiming success', async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockResolvedValue(
      mockFetchResponse({ status: 401, body: { msg: 'invalid token' } }),
    );
    render(<RecoveryConfirmScreen accessToken="recovery-jwt" onDone={vi.fn()} />);

    await user.type(screen.getByLabelText(/^New password$/i), 'BrandNewPassword42!');
    await user.type(screen.getByLabelText(/Confirm new password/i), 'BrandNewPassword42!');
    await user.click(screen.getByRole('button', { name: /Update password/i }));

    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: /Password updated/i })).not.toBeInTheDocument();
    });
    expect(screen.getByRole('heading', { name: /Set a new password/i })).toBeInTheDocument();
  });
});

// ===========================================================================
// LoginScreen
// ===========================================================================
describe('LoginScreen', () => {
  const noop = () => {};

  it('blocks submit and shows field errors on empty input', async () => {
    const user = userEvent.setup();
    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.click(screen.getByRole('button', { name: /Sign in/i }));
    expect(mockAuth.signIn).not.toHaveBeenCalled();
    expect(screen.getByText(/Enter your email address/i)).toBeInTheDocument();
  });

  it('calls signIn with trimmed credentials on valid submit', async () => {
    const user = userEvent.setup();
    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), '  user@example.com  ');
    await user.type(screen.getByLabelText(/^Password$/i), 'secret123');
    await user.click(screen.getByRole('button', { name: /Sign in/i }));
    await waitFor(() => expect(mockAuth.signIn).toHaveBeenCalledWith('user@example.com', 'secret123'));
  });

  it('surfaces an object-shaped auth error to the user', async () => {
    const user = userEvent.setup();
    mockAuth.signIn = vi.fn(async () => ({
      ok: false,
      error: { message: 'Invalid email or password.', code: 'invalid_credentials' },
    }));
    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'user@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'wrongpass');
    await user.click(screen.getByRole('button', { name: /Sign in/i }));
    expect(await screen.findByText('Invalid email or password.')).toBeInTheDocument();
  });

  it('renders sanitized invalid_credentials after the pending state without a React-managed button style', async () => {
    const user = userEvent.setup();
    let resolveSignIn;
    mockAuth.signIn = vi.fn(() => new Promise((resolve) => {
      resolveSignIn = resolve;
    }));

    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'typo@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'wrong-password');
    await user.click(screen.getByRole('button', { name: /^Sign in$/i }));

    const busyButton = await screen.findByRole('button', { name: /Signing in/i });
    expect(busyButton).toBeDisabled();
    expect(busyButton.querySelector('style')).toBeNull();
    expect(busyButton.querySelector('svg')).not.toBeNull();

    await act(async () => {
      resolveSignIn({
        ok: false,
        error: {
          code: 'invalid_credentials',
          status: 400,
          message: "We couldn't sign you in. Check your email and password.",
        },
      });
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "We couldn't sign you in. Check your email and password.",
    );
    expect(screen.getByRole('button', { name: /^Sign in$/i })).toBeEnabled();
  });

  // --- Second factor (#2404) -------------------------------------------------
  // A TOTP-enrolled account: signIn returns mfaRequired and the provider holds
  // mfaPending, so the screen must prompt for the code (never show the
  // dashboard) and complete the step-up through verifyMfaTotp.
  it('shows the TOTP prompt instead of an error when signIn returns mfaRequired', async () => {
    const user = userEvent.setup();
    mockAuth.signIn = vi.fn(async () => {
      mockAuth.mfaPending = true;
      mockAuth.mfaFactorId = 'factor-1';
      return { ok: false, mfaRequired: true, factorId: 'factor-1', error: null };
    });
    const { rerender } = render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'mfa@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'secret123');
    await user.click(screen.getByRole('button', { name: /^Sign in$/i }));
    // Re-render so the screen sees the provider's now-pending state.
    rerender(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);

    expect(await screen.findByLabelText(/Authentication code/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Verify/i })).toBeInTheDocument();
    // No sign-in error was surfaced (mfaRequired is not a failure).
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('completes sign-in by calling verifyMfaTotp with the entered code', async () => {
    const user = userEvent.setup();
    mockAuth.mfaPending = true;
    mockAuth.mfaFactorId = 'factor-1';
    mockAuth.verifyMfaTotp = vi.fn(async () => ({ ok: true, error: null }));
    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.type(screen.getByLabelText(/Authentication code/i), '123456');
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    await waitFor(() => expect(mockAuth.verifyMfaTotp).toHaveBeenCalledWith('123456'));
  });

  it('keeps the prompt up and shows an error when the code is rejected', async () => {
    const user = userEvent.setup();
    mockAuth.mfaPending = true;
    mockAuth.mfaFactorId = 'factor-1';
    mockAuth.verifyMfaTotp = vi.fn(async () => ({
      ok: false,
      error: { message: 'That code is not valid. Check your authenticator app and try again.', code: 'mfa_verification_failed' },
    }));
    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.type(screen.getByLabelText(/Authentication code/i), '000000');
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/code is not valid/i);
    // Still on the TOTP prompt, not signed in.
    expect(screen.getByLabelText(/Authentication code/i)).toBeInTheDocument();
  });

  it('revokes the half-authenticated session via cancelMfa on "Back to sign in"', async () => {
    const user = userEvent.setup();
    mockAuth.mfaPending = true;
    mockAuth.mfaFactorId = 'factor-1';
    mockAuth.cancelMfa = vi.fn(async () => ({ ok: true, error: null }));
    render(<LoginScreen onSwitchToSignUp={noop} onForgotPassword={noop} onResendVerification={noop} />);
    await user.click(screen.getByRole('button', { name: /Back to sign in/i }));
    await waitFor(() => expect(mockAuth.cancelMfa).toHaveBeenCalled());
  });

  // #2153: before this fix, a returning user who never verified had no path
  // back to a resend action at all — LoginScreen had no such link, so this
  // is RED against the pre-fix markup (no "Resend verification email?" role)
  // and GREEN once the link exists and forwards the typed email untouched.
  it('offers a self-service "Resend verification email?" link that forwards the typed email', async () => {
    const user = userEvent.setup();
    const onResendVerification = vi.fn();
    render(
      <LoginScreen
        onSwitchToSignUp={noop}
        onForgotPassword={noop}
        onResendVerification={onResendVerification}
      />,
    );
    await user.type(screen.getByLabelText(/^Email$/i), '  stuck@example.com  ');
    await user.click(screen.getByRole('button', { name: /Resend verification email/i }));
    expect(onResendVerification).toHaveBeenCalledWith('stuck@example.com');
  });
});

// ===========================================================================
// SignUpScreen
// ===========================================================================
describe('SignUpScreen', () => {
  const noop = () => {};

  it('rejects mismatched passwords without calling signUp', async () => {
    const user = userEvent.setup();
    render(<SignUpScreen onSwitchToLogin={noop} onNeedsVerification={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'new@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'longenough123');
    await user.type(screen.getByLabelText(/Confirm password/i), 'different123');
    await user.click(screen.getByLabelText(/age and guardian-consent requirements/i));
    await user.click(screen.getByLabelText(/I agree to Viola's/i));
    await user.click(screen.getByRole('button', { name: /Create account/i }));
    expect(mockAuth.signUp).not.toHaveBeenCalled();
    expect(screen.getByText(/Passwords do not match/i)).toBeInTheDocument();
  });

  it('requires eligibility and terms acceptance before signup can submit', async () => {
    const user = userEvent.setup();
    render(<SignUpScreen onSwitchToLogin={noop} onNeedsVerification={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'new@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'longenough123');
    await user.type(screen.getByLabelText(/Confirm password/i), 'longenough123');
    expect(screen.getByRole('button', { name: /Create account/i })).toBeDisabled();
  });

  it('hands the email up when verification is required', async () => {
    const user = userEvent.setup();
    const onNeedsVerification = vi.fn();
    mockAuth.signUp = vi.fn(async () => ({ ok: true, needsEmailVerification: true, error: null }));
    render(<SignUpScreen onSwitchToLogin={noop} onNeedsVerification={onNeedsVerification} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'new@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'longenough123');
    await user.type(screen.getByLabelText(/Confirm password/i), 'longenough123');
    await user.click(screen.getByLabelText(/age and guardian-consent requirements/i));
    await user.click(screen.getByLabelText(/I agree to Viola's/i));
    await user.click(screen.getByRole('button', { name: /Create account/i }));
    await waitFor(() => expect(onNeedsVerification).toHaveBeenCalledWith('new@example.com'));
    expect(mockAuth.signUp).toHaveBeenCalledWith('new@example.com', 'longenough123', {
      tosAccepted: true,
      legalEligibilityConfirmed: true,
      termsVersion: ACCEPTED_TERMS_VERSION,
      privacyVersion: ACCEPTED_PRIVACY_VERSION,
    });
  });
});

// ===========================================================================
// ResetPasswordScreen
// ===========================================================================
describe('ResetPasswordScreen', () => {
  const noop = () => {};

  it('shows an enumeration-safe confirmation after a successful request', async () => {
    const user = userEvent.setup();
    render(<ResetPasswordScreen onBackToLogin={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'user@example.com');
    await user.click(screen.getByRole('button', { name: /Send reset link/i }));
    expect(await screen.findByRole('heading', { name: /Check your inbox/i })).toBeInTheDocument();
    expect(mockAuth.resetPassword).toHaveBeenCalledWith('user@example.com');
  });

  it('shows an error when the reset request fails', async () => {
    const user = userEvent.setup();
    mockAuth.resetPassword = vi.fn(async () => ({
      ok: false,
      error: { message: 'Service unavailable.', code: 'server_error' },
    }));
    render(<ResetPasswordScreen onBackToLogin={noop} />);
    await user.type(screen.getByLabelText(/^Email$/i), 'user@example.com');
    await user.click(screen.getByRole('button', { name: /Send reset link/i }));
    expect(await screen.findByText('Service unavailable.')).toBeInTheDocument();
  });
});

// ===========================================================================
// VerifyEmailScreen
// ===========================================================================
describe('VerifyEmailScreen', () => {
  const noop = () => {};

  it('shows the target email and resends verification on demand', async () => {
    const user = userEvent.setup();
    render(<VerifyEmailScreen email="new@example.com" onBackToLogin={noop} />);
    expect(screen.getByText(/new@example.com/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Resend verification email/i }));
    await waitFor(() => expect(mockAuth.resendVerification).toHaveBeenCalledWith('new@example.com'));
    expect(await screen.findByText(/Verification email sent/i)).toBeInTheDocument();
  });

  it('returns to sign in from the verify screen', async () => {
    const user = userEvent.setup();
    const onBackToLogin = vi.fn();
    render(<VerifyEmailScreen email="new@example.com" onBackToLogin={onBackToLogin} />);
    await user.click(screen.getByRole('button', { name: /go to sign in/i }));
    expect(onBackToLogin).toHaveBeenCalled();
  });

  // #2153 self-service path: reached with no known email (from login's
  // resend link), the screen must collect one itself and never assert
  // account existence in its own success copy (mirrors the backend's
  // enumeration-safe /resend response — this text must stay generic even if
  // a future edit reshapes the backend message again).
  it('collects an email itself and resends with an enumeration-safe confirmation when none is known', async () => {
    const user = userEvent.setup();
    render(<VerifyEmailScreen initialEmail="stuck@example.com" onBackToLogin={noop} />);
    const emailField = screen.getByLabelText(/^Email$/i);
    expect(emailField).toHaveValue('stuck@example.com');
    await user.click(screen.getByRole('button', { name: /Resend verification email/i }));
    await waitFor(() => expect(mockAuth.resendVerification).toHaveBeenCalledWith('stuck@example.com'));
    expect(await screen.findByText(/If an account exists with that email/i)).toBeInTheDocument();
    expect(screen.queryByText(/^Verification email sent/i)).not.toBeInTheDocument();
  });

  it('validates the self-entered email before calling resendVerification', async () => {
    const user = userEvent.setup();
    render(<VerifyEmailScreen onBackToLogin={noop} />);
    await user.click(screen.getByRole('button', { name: /Resend verification email/i }));
    expect(mockAuth.resendVerification).not.toHaveBeenCalled();
    expect(screen.getByText(/Enter your email address/i)).toBeInTheDocument();
  });
});
