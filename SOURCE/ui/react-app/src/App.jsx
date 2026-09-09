import SmartDisplay from './SmartDisplay';
import { useEffect } from 'react';
import PropTypes from 'prop-types';
// New cloud-account auth (GoTrue session) — powers the surface-aware front
// door (CloudAuthGate). Lives under src/auth/.
import { AuthProvider as CloudAccountProvider } from './auth/AuthProvider';
import { useAuth as useCloudAccountAuth } from './auth/useAuth';
// Established app-wide auth context (plan/subscription/OAuth) consumed by
// SmartDisplay, AccountTab, useUsage, ReviewPage, etc. via ./hooks/useAuth.
// Kept mounted so those consumers keep working unchanged.
import { AuthProvider as AppAuthProvider, useAuth as useAppAuth } from './hooks/useAuth';
import { UiStateProvider } from './state/uiState';
import SpokeWrapper from './components/SpokeWrapper';
import SpokePairingGate from './components/SpokePairingGate';
import SpokeSessionBoundary from './components/SpokeSessionBoundary';
import ReviewPage from './components/ReviewPage';
import { CloudAuthGate } from './components/auth';
import { isCloudSurface } from './components/auth/cloudSurface';
import { syncSentryUser } from './sentryClient';

// Detect mode: ?mode=review renders the review page.
// ?mode=speaker, ?room=, and scoped ?spoke_token= links route through
// SpokeWrapper so spokes render
// the full SmartDisplay (identical to desktop) on top of the hub-streamed
// PCM audio engine. The legacy minimal SpokeAudioPage is retired.
const params = new URLSearchParams(window.location.search);
const mode = params.get('mode');
const room = params.get('room');
const spokeToken = params.get('spoke_token');
// Scanned Add Room QR: a short-lived, single-use pairing ticket. It is NOT a
// credential — the device swaps it for one at /bootstrap/claim (#4434).
const pairTicket = params.get('pair');

function SentryUserBridge({ children }) {
  const cloudAuth = useCloudAccountAuth();
  const appAuth = useAppAuth();
  const user = cloudAuth?.user || appAuth?.user || null;

  useEffect(() => {
    syncSentryUser(user);
  }, [user]);

  return children;
}

SentryUserBridge.propTypes = {
  children: PropTypes.node.isRequired,
};

/**
 * Surface-aware dashboard entry.
 *
 * On the CLOUD surface a browser visitor has no desktop API key, so the
 * dashboard is gated behind a Viola Cloud account: CloudAuthGate shows the
 * login / sign-up front door until `useAuth().status === 'signedIn'`, then
 * renders SmartDisplay.
 *
 * On the DESKTOP surface the existing `__VIOLA_API_KEY__` gate in main.jsx
 * already vouched for the client, so SmartDisplay renders directly — the
 * desktop behavior is unchanged.
 *
 * SmartDisplay is always wrapped in AppAuthProvider so its `useAuth()` /
 * `usePlan()` consumers keep their context regardless of surface.
 */
function Dashboard() {
  if (isCloudSurface()) {
    return (
      <CloudAuthGate>
        <AppAuthProvider>
          <SentryUserBridge>
            <SmartDisplay />
          </SentryUserBridge>
        </AppAuthProvider>
      </CloudAuthGate>
    );
  }
  return (
    <AppAuthProvider>
      <SentryUserBridge>
        <SmartDisplay />
      </SentryUserBridge>
    </AppAuthProvider>
  );
}

export default function App() {
  if (mode === 'review') {
    return (
      <UiStateProvider>
        <AppAuthProvider>
          <ReviewPage />
        </AppAuthProvider>
      </UiStateProvider>
    );
  }

  if (mode === 'speaker' || room || spokeToken || pairTicket) {
    // A spoke device that reached the hub WITHOUT a credential must earn one:
    // the scanned-QR path arrives with a single-use ?pair= ticket the gate
    // exchanges automatically, and the typed pairing-word path lands at
    // /?room=<room> with nothing, so the user enters the PIN shown on the
    // desktop. The gate is LAN-local (it drives the hub's desktop-only
    // /bootstrap pairing routes); on the cloud surface there is no LAN hub to
    // pair with, so it is bypassed.
    if (!spokeToken && !isCloudSurface()) {
      return (
        <UiStateProvider>
          <SpokePairingGate room={room || 'speaker'} ticket={pairTicket || ''} />
        </UiStateProvider>
      );
    }
    return (
      <UiStateProvider>
        <SpokeSessionBoundary room={room || 'speaker'} spokeToken={spokeToken || ''}>
          <SpokeWrapper room={room || 'speaker'} />
        </SpokeSessionBoundary>
      </UiStateProvider>
    );
  }

  return (
    <UiStateProvider>
      <CloudAccountProvider>
        <Dashboard />
      </CloudAccountProvider>
    </UiStateProvider>
  );
}
