import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import OnboardingOverlay, { ATTRIBUTION_OPTIONS } from './OnboardingOverlay';
import '../i18n';

const baseProps = {
  isOnboarding: true,
  phase: 'mic_try',
  phaseIndex: 4,
  totalPhases: 6,
  phaseContent: 'Last step: hold the microphone button and ask Viola something.',
  isSpeaking: false,
  suggestions: [
    { text: 'what is two plus two', label: 'What is two plus two' },
  ],
  isAccountPairPhase: false,
  isMicPermissionPhase: false,
  micPermission: { checking: false, blocked: false },
  isCloudConsentPhase: false,
  cloudConsentStatus: 'idle',
  cloudConsentError: null,
  isAutonomyPhase: false,
  autonomyStatus: 'idle',
  autonomyError: null,
  tryCommandStatus: 'idle',
  skipOnboarding: vi.fn(),
  onSuggestionTap: vi.fn(),
  sendCommand: vi.fn(),
};

describe('OnboardingOverlay accessibility', () => {
  it('renders onboarding as a named dialog with status and progress semantics', () => {
    render(<OnboardingOverlay {...baseProps} />);

    expect(screen.getByRole('dialog', { name: /say hello to viola/i })).toBeInTheDocument();
    expect(screen.getByText(/hold the microphone button/i).closest('[role="status"]')).not.toBeNull();
    expect(screen.getByRole('progressbar', { name: /onboarding progress/i })).toHaveAttribute('aria-valuenow', '5');
    expect(screen.getByRole('button', { name: /try command: what is two plus two/i })).toBeInTheDocument();
  });

  it('announces inline onboarding errors', () => {
    render(
      <OnboardingOverlay
        {...baseProps}
        phase="cloud_consent"
        phaseIndex={2}
        isCloudConsentPhase
        cloudConsentStatus="error"
        cloudConsentError="Could not save settings"
        suggestions={null}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('Could not save settings');
  });
});

describe('OnboardingOverlay welcome phase (the beta thank-you opener)', () => {
  const welcomeProps = {
    ...baseProps,
    phase: 'welcome',
    phaseIndex: 0,
    totalPhases: 6,
    phaseContent: 'Thank you so much for trying Viola. We are still in beta.',
    suggestions: null,
      isWelcomePhase: true,
  };

  it('greets by name and offers a begin action plus a bug-report affordance', () => {
    const onWelcomeContinue = vi.fn();
    const onReportBug = vi.fn();
    render(
      <OnboardingOverlay
        {...welcomeProps}
        onWelcomeContinue={onWelcomeContinue}
        onReportBug={onReportBug}
      />,
    );

    expect(screen.getByRole('dialog', { name: /thank you for trying viola/i })).toBeInTheDocument();
    expect(screen.getByText(/still in beta/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /begin setup/i }));
    expect(onWelcomeContinue).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: /report a bug/i }));
    expect(onReportBug).toHaveBeenCalledTimes(1);
  });

  it('shows the WELCOME eyebrow + BETA badge only on the welcome step', () => {
    const { rerender } = render(<OnboardingOverlay {...welcomeProps} onWelcomeContinue={vi.fn()} />);
    // Welcome step: eyebrow reads "Welcome" and the Beta badge is present.
    expect(screen.getByText('Welcome')).toBeInTheDocument();
    expect(screen.getByText('Beta')).toBeInTheDocument();

    // A later step: the persistent "Viola" wordmark, no Beta badge, no "Welcome".
    rerender(
      <OnboardingOverlay
        {...baseProps}
        isWelcomePhase={false}
        suggestions={null}
      />,
    );
    expect(screen.getByText('Viola')).toBeInTheDocument();
    expect(screen.queryByText('Beta')).toBeNull();
    expect(screen.queryByText('Welcome')).toBeNull();
  });

  it('omits the bug-report button when no reporter is wired', () => {
    render(<OnboardingOverlay {...welcomeProps} onWelcomeContinue={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /report a bug/i })).toBeNull();
  });
});

describe('OnboardingOverlay account sign-in phase', () => {
  const signInProps = {
    ...baseProps,
    phase: 'account_pair',
    phaseIndex: 1,
    totalPhases: 6,
    phaseContent: 'First, connect this app to your Viola account.',
    suggestions: null,
      isAccountPairPhase: true,
    signInStatus: 'awaiting_user',
  };

  it('points at Account settings (Path A login) and never renders a pair code', () => {
    const onOpenSettings = vi.fn();
    render(<OnboardingOverlay {...signInProps} onOpenSettings={onOpenSettings} />);

    fireEvent.click(screen.getByRole('button', { name: /open account settings/i }));
    expect(onOpenSettings).toHaveBeenCalledWith('account');
    // The device-pair code dance was removed with Path A cloud-proxy login;
    // its UI must not resurface.
    expect(screen.queryByText(/useviola\.com\/activate/i)).toBeNull();
    expect(screen.queryByText(/your code/i)).toBeNull();
  });

  it('shows the signed-in confirmation while advancing', () => {
    render(<OnboardingOverlay {...signInProps} signInStatus="signed_in" />);

    expect(screen.getByText(/signed in\. continuing setup/i)).toBeInTheDocument();
  });

  // #4225: the account step used to render exactly one action, so a user who
  // did not want a cloud account had no way forward at all. Viola's runtime
  // only requires an account for managed AI (core/account_gate.py exempts
  // BYOK / Codex / local), so first run must offer the same escape.
  it('offers a guest path alongside sign-in and reports the choice', () => {
    const onContinueWithoutAccount = vi.fn();
    render(
      <OnboardingOverlay
        {...signInProps}
        onOpenSettings={vi.fn()}
        onContinueWithoutAccount={onContinueWithoutAccount}
      />,
    );

    // Sign-in is still offered and still primary; the guest path is the
    // secondary action, not a replacement.
    expect(screen.getByRole('button', { name: /open account settings/i })).toBeInTheDocument();

    const guest = screen.getByRole('button', { name: /continue without an account/i });
    expect(guest).toBeInTheDocument();
    fireEvent.click(guest);
    expect(onContinueWithoutAccount).toHaveBeenCalledTimes(1);

    // And the panel says plainly what an account is actually for.
    expect(screen.getByText(/only needed for viola-managed ai/i)).toBeInTheDocument();
  });

  it('drops the guest path once the user is signed in', () => {
    render(<OnboardingOverlay {...signInProps} signInStatus="signed_in" />);

    expect(screen.queryByRole('button', { name: /continue without an account/i })).toBeNull();
  });
});

describe('OnboardingOverlay AI step for a user with no account (#4225)', () => {
  const guestCloudProps = {
    ...baseProps,
    phase: 'cloud_consent',
    phaseIndex: 2,
    totalPhases: 6,
    phaseContent: 'Choose AI processing.',
    suggestions: null,
    isCloudConsentPhase: true,
    cloudConsentStatus: 'idle',
    cloudConsentError: null,
    accountSkipped: true,
  };

  it('never offers managed AI, which cannot work without an account', () => {
    render(<OnboardingOverlay {...guestCloudProps} onChoice={vi.fn()} onCloudConsentChoice={vi.fn()} />);

    expect(screen.queryByRole('button', { name: /enable cloud ai/i })).toBeNull();
    expect(screen.getByText(/viola-managed ai is not available/i)).toBeInTheDocument();
  });

  it('offers the paths that do work, and a way back to signing in', () => {
    const onCloudConsentChoice = vi.fn();
    const onReturnToSignIn = vi.fn();
    render(
      <OnboardingOverlay
        {...guestCloudProps}
        onCloudConsentChoice={onCloudConsentChoice}
        onReturnToSignIn={onReturnToSignIn}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /use my own ai key/i }));
    expect(onCloudConsentChoice).toHaveBeenCalledWith('decline');

    fireEvent.click(screen.getByRole('button', { name: /sign in after all/i }));
    expect(onReturnToSignIn).toHaveBeenCalledTimes(1);
  });

  it('still shows managed AI to a user who has not opted out of an account', () => {
    render(
      <OnboardingOverlay
        {...guestCloudProps}
        accountSkipped={false}
        onCloudConsentChoice={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: /enable cloud ai/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign in after all/i })).toBeNull();
  });
});

describe('OnboardingOverlay is dismissible from every phase (#4225)', () => {
  const PHASES = [
    ['welcome', 0],
    ['account_pair', 1],
    ['cloud_consent', 2],
    ['autonomy_tier', 3],
    ['mic_try', 4],
    ['hear_about', 5],
  ];

  it.each(PHASES)('renders a working exit on the %s phase', (phase, phaseIndex) => {
    const skipOnboarding = vi.fn();
    render(
      <OnboardingOverlay
        {...baseProps}
        phase={phase}
        phaseIndex={phaseIndex}
        suggestions={null}
        skipOnboarding={skipOnboarding}
      />,
    );

    const exit = screen.getByRole('button', { name: /skip introduction/i });
    fireEvent.click(exit);
    expect(skipOnboarding).toHaveBeenCalledTimes(1);
  });

  it('keeps the exit inside the dialog so keyboard users can reach it', () => {
    render(<OnboardingOverlay {...baseProps} phase="account_pair" phaseIndex={1} suggestions={null} />);

    const dialog = screen.getByRole('dialog');
    const exit = screen.getByRole('button', { name: /skip introduction/i });
    // The Tab trap spans the dialog root, so an exit rendered outside it would
    // be unreachable by keyboard - the one exit from first run must not be.
    expect(dialog.contains(exit)).toBe(true);
    expect(exit).not.toHaveAttribute('tabindex', '-1');
  });
});

describe('OnboardingOverlay autonomy-tier phase (solo / ensemble / symphony)', () => {
  const autonomyProps = {
    ...baseProps,
    phase: 'autonomy_tier',
    phaseIndex: 3,
    totalPhases: 6,
    phaseContent: 'Choose how much Viola is allowed to do on this computer.',
    suggestions: null,
      isAutonomyPhase: true,
    autonomyStatus: 'idle',
    autonomyError: null,
  };

  it('offers all three honest tiers presented neutrally (none recommended or pre-selected)', () => {
    render(<OnboardingOverlay {...autonomyProps} onAutonomyChoice={vi.fn()} />);

    for (const tier of ['solo', 'ensemble', 'symphony']) {
      expect(screen.getByTestId(`autonomy-${tier}`)).toBeInTheDocument();
      // No tier is pre-selected; the user makes the trust call themselves.
      expect(screen.getByTestId(`autonomy-${tier}`)).toHaveAttribute('aria-checked', 'false');
    }
    // No "recommended" nudge is shown.
    expect(screen.queryByText(/recommended/i)).not.toBeInTheDocument();
    // Honest capability copy is present (no vague marketing).
    expect(screen.getByText(/full access/i)).toBeInTheDocument();
    expect(screen.getByText(/no file access/i)).toBeInTheDocument();
  });

  it('reports the chosen tier value', () => {
    const onAutonomyChoice = vi.fn();
    render(<OnboardingOverlay {...autonomyProps} onAutonomyChoice={onAutonomyChoice} />);

    fireEvent.click(screen.getByTestId('autonomy-symphony'));
    expect(onAutonomyChoice).toHaveBeenCalledWith('symphony');
  });

  it('announces a failed save', () => {
    render(
      <OnboardingOverlay
        {...autonomyProps}
        autonomyStatus="error"
        autonomyError="Couldn't save that choice."
        onAutonomyChoice={vi.fn()}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't save that choice.");
  });
});

describe('OnboardingOverlay attribution phase ("How did you hear about Viola?")', () => {
  const attributionProps = {
    ...baseProps,
    phase: 'hear_about',
    phaseIndex: 5,
    totalPhases: 6,
    phaseContent: 'One quick, optional question: how did you hear about Viola?',
    suggestions: null,
    isAttributionPhase: true,
    attributionStatus: 'idle',
  };

  it('offers exactly the closed funnel enum plus "prefer not to say"', () => {
    render(<OnboardingOverlay {...attributionProps} onAttributionChoice={vi.fn()} />);

    // Every choice is one of the server-side FUNNEL_ATTRIBUTION_OPTIONS values.
    const expectedValues = ['reddit', 'x', 'youtube', 'tiktok', 'hn', 'search', 'friend', 'blog', 'other'];
    expect(ATTRIBUTION_OPTIONS.map((o) => o.value)).toEqual(expectedValues);
    for (const value of expectedValues) {
      expect(screen.getByTestId(`attribution-${value}`)).toBeInTheDocument();
    }
    // No free-text entry: the answer can never carry PII.
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByRole('button', { name: /prefer not to say/i })).toBeInTheDocument();
  });

  it('reports the chosen closed-enum value', () => {
    const onAttributionChoice = vi.fn();
    render(<OnboardingOverlay {...attributionProps} onAttributionChoice={onAttributionChoice} />);

    fireEvent.click(screen.getByTestId('attribution-reddit'));
    expect(onAttributionChoice).toHaveBeenCalledWith('reddit');
  });

  it('"prefer not to say" reports the empty (unanswered) value', () => {
    const onAttributionChoice = vi.fn();
    render(<OnboardingOverlay {...attributionProps} onAttributionChoice={onAttributionChoice} />);

    fireEvent.click(screen.getByRole('button', { name: /prefer not to say/i }));
    expect(onAttributionChoice).toHaveBeenCalledWith('');
  });
});
