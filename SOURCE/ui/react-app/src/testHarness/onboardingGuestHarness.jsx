/**
 * Test-only mount for the #4225 hermetic Playwright proof. NOT imported by
 * main.jsx/App.jsx/SmartDisplay.jsx -- production wiring is untouched.
 *
 * Wires the real OnboardingOverlay.jsx + useVoiceOnboarding.js exactly the way
 * SmartDisplay.jsx wires them (same prop names, same order, same handlers), so
 * this harness drives the actual production first-run contract in a real
 * browser DOM rather than a jsdom approximation. Everything the user touches
 * here -- the phase machine, the guest action, the AI-step routing, the
 * dismiss control, the focus trap -- is the shipped code.
 *
 * What is NOT real: the desktop `/v1/*` endpoints behind it, which
 * tests/e2e/web/onboarding_guest/backend_harness.py serves from memory. The
 * desktop route handlers read and write the process-global SettingsManager,
 * and this sandbox has no desktop app to host it (see
 * docs/runbooks/CLOUD_SESSION_RUNBOOK.md). That boundary is fine for #4225:
 * the bug and the fix are entirely client-side, so the endpoints only need to
 * answer honestly and record what the client sent -- which the harness
 * mirrors back into the DOM so the proof can assert on it.
 *
 * See onboarding-guest-harness.html for how this gets served (a standalone
 * bundle built by vite.onboarding-harness.config.js, never `npm run build`).
 */
import React, { useCallback, useState } from 'react';
import ReactDOM from 'react-dom/client';
import OnboardingOverlay from '../components/OnboardingOverlay';
import { useVoiceOnboarding } from '../hooks/useVoiceOnboarding';

function Harness() {
  const onboarding = useVoiceOnboarding();
  const [settingsOpened, setSettingsOpened] = useState('none');

  // The proof reads the backend's own record over HTTP (GET /harness/state)
  // rather than mirroring it into the DOM here. A poller in this component
  // would keep the page permanently non-idle, which is both a worse assertion
  // surface and enough to hang Playwright's networkidle wait.

  // SmartDisplay opens the settings modal here. There is no settings modal in
  // this harness, so record the request instead -- that is enough to prove the
  // guest path never routed the user through sign-in.
  const handleOpenSettings = useCallback((tab) => {
    setSettingsOpened(String(tab));
  }, []);

  return (
    <div
      style={{
        padding: 24,
        fontFamily: 'sans-serif',
        color: '#fff',
        background: '#000',
        minHeight: '100vh',
      }}
    >
      <div data-testid="harness-status">
        onboarding={String(onboarding.isOnboarding)} phase={String(onboarding.phase)}
        {' '}signInStatus={String(onboarding.signInStatus)}
        {' '}accountSkipped={String(onboarding.accountSkipped)}
        {' '}settingsOpened={settingsOpened}
      </div>

      <OnboardingOverlay
        isOnboarding={onboarding.isOnboarding}
        phase={onboarding.phase}
        phaseIndex={onboarding.phaseIndex}
        totalPhases={onboarding.totalPhases}
        phaseContent={onboarding.phaseContent}
        isSpeaking={onboarding.isSpeaking}
        isWelcomePhase={onboarding.isWelcomePhase}
        onWelcomeContinue={onboarding.onWelcomeContinue}
        onReportBug={() => {}}
        isAccountPairPhase={onboarding.isAccountPairPhase}
        signInStatus={onboarding.signInStatus}
        accountSkipped={onboarding.accountSkipped}
        onContinueWithoutAccount={onboarding.onContinueWithoutAccount}
        onReturnToSignIn={onboarding.onReturnToSignIn}
        isMicPermissionPhase={onboarding.isMicPermissionPhase}
        micPermission={onboarding.micPermission}
        onMicPermissionRetry={onboarding.onMicPermissionRetry}
        onSkipMicStep={onboarding.onSkipMicStep}
        isCloudConsentPhase={onboarding.isCloudConsentPhase}
        cloudConsentStatus={onboarding.cloudConsentStatus}
        cloudConsentError={onboarding.cloudConsentError}
        onCloudConsentChoice={onboarding.onCloudConsentChoice}
        onByokSetupDone={onboarding.onByokSetupDone}
        isAutonomyPhase={onboarding.isAutonomyPhase}
        autonomyStatus={onboarding.autonomyStatus}
        autonomyError={onboarding.autonomyError}
        onAutonomyChoice={onboarding.onAutonomyChoice}
        isAttributionPhase={onboarding.isAttributionPhase}
        attributionStatus={onboarding.attributionStatus}
        onAttributionChoice={onboarding.onAttributionChoice}
        tryCommandStatus={onboarding.tryCommandStatus}
        suggestions={onboarding.suggestions}
        skipOnboarding={onboarding.skipOnboarding}
        onSuggestionTap={onboarding.onSuggestionTap}
        sendCommand={() => Promise.resolve()}
        onOpenSettings={handleOpenSettings}
      />
    </div>
  );
}

const rootElement = document.getElementById('root');
if (rootElement) {
  ReactDOM.createRoot(rootElement).render(<Harness />);
}
