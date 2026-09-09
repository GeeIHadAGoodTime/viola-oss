/**
 * CloudAuthGate — the Viola Cloud front door.
 *
 * On the cloud surface, an unauthenticated browser visitor must sign into a
 * Viola account before reaching the dashboard. This component owns that
 * decision:
 *
 *   useAuth().status === 'loading'   -> branded loading screen
 *   useAuth().status === 'signedOut' -> login / sign-up / reset / verify
 *   useAuth().status === 'signedIn'  -> render children (the dashboard)
 *
 * Which signed-out screen shows is local view state (login by default; the
 * screens hand control to each other). Once the AuthProvider establishes a
 * session it flips `status` to 'signedIn' and the dashboard renders — no
 * routing needed.
 *
 * This component is ONLY mounted on the cloud surface. The desktop surface
 * keeps its existing `__VIOLA_API_KEY__` gate untouched.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import LoginScreen from './LoginScreen';
import SignUpScreen from './SignUpScreen';
import ResetPasswordScreen from './ResetPasswordScreen';
import RecoveryConfirmScreen from './RecoveryConfirmScreen';
import VerifyEmailScreen from './VerifyEmailScreen';
import AuthLoadingScreen from './AuthLoadingScreen';
import { useAuth } from '../../auth/useAuth';
import { parseRecoveryHash } from '../../auth/authClient';

/**
 * Strip the recovery session out of the URL fragment once the flow is done, so
 * the tokens don't linger in history / get re-parsed on a re-render. Keeps the
 * path + query intact.
 */
function clearRecoveryHash() {
  if (typeof window === 'undefined' || !window.history?.replaceState) return;
  const { pathname, search } = window.location;
  window.history.replaceState(null, '', `${pathname}${search}`);
}

// Signed-out sub-views.
const VIEW = {
  LOGIN: 'login',
  SIGNUP: 'signup',
  RESET: 'reset',
  VERIFY: 'verify',
};

export default function CloudAuthGate({ children }) {
  const { status } = useAuth();
  const [view, setView] = useState(VIEW.LOGIN);
  // Carries the address from sign-up -> verify, where it is a CONFIRMED
  // address (the account was just created with it) — VerifyEmailScreen shows
  // it as fixed, non-editable text.
  const [pendingEmail, setPendingEmail] = useState('');
  // A recovery-email link lands here (on the SPA, signed out) with the recovery
  // session in the URL fragment. Detect it once at mount; while it is set, the
  // set-new-password screen takes precedence over every other front-door view
  // and the loading/signed-in branches (#2608).
  const [recovery, setRecovery] = useState(() => parseRecoveryHash());

  // Carries the address from login's "Resend verification email?" link ->
  // verify, for a returning user who never verified and lost or missed the
  // original link (#2153, CL-20260716-6e27's affected-user shape). This is
  // just whatever the user had typed into the login form — unconfirmed, so
  // VerifyEmailScreen shows it as an editable, pre-filled field instead.
  const [resendPrefillEmail, setResendPrefillEmail] = useState('');

  if (recovery) {
    return (
      <RecoveryConfirmScreen
        accessToken={recovery.accessToken}
        onDone={() => {
          clearRecoveryHash();
          setRecovery(null);
          setView(VIEW.LOGIN);
        }}
      />
    );
  }

  if (status === 'loading') {
    return <AuthLoadingScreen />;
  }

  if (status === 'signedIn') {
    return children;
  }

  // status === 'signedOut' — render the requested front-door screen.
  switch (view) {
    case VIEW.SIGNUP:
      return (
        <SignUpScreen
          onSwitchToLogin={() => setView(VIEW.LOGIN)}
          onNeedsVerification={(email) => {
            setPendingEmail(email);
            setView(VIEW.VERIFY);
          }}
        />
      );
    case VIEW.RESET:
      return (
        <ResetPasswordScreen
          initialEmail={pendingEmail}
          onBackToLogin={() => setView(VIEW.LOGIN)}
        />
      );
    case VIEW.VERIFY:
      return (
        <VerifyEmailScreen
          email={pendingEmail || undefined}
          initialEmail={resendPrefillEmail}
          onBackToLogin={() => setView(VIEW.LOGIN)}
        />
      );
    case VIEW.LOGIN:
    default:
      return (
        <LoginScreen
          onSwitchToSignUp={() => setView(VIEW.SIGNUP)}
          onForgotPassword={() => setView(VIEW.RESET)}
          onResendVerification={(email) => {
            setPendingEmail('');
            setResendPrefillEmail(email);
            setView(VIEW.VERIFY);
          }}
        />
      );
  }
}

CloudAuthGate.propTypes = {
  children: PropTypes.node.isRequired,
};

export { VIEW as CLOUD_AUTH_VIEW };
