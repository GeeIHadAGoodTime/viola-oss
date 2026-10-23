/**
 * First run must leave the compositor idle while the user reads.
 *
 * 1.0.5 build 3 laptop report: during all of onboarding the screen alternated
 * between the onboarding card and the stage (71 switches in 19.6 s) and
 * onboarding idled at 0.62-0.72 cores against 0.107 on the stage. React never
 * unmounted the overlay (a MutationObserver on the frozen build saw zero DOM
 * changes); the trigger was the overlay itself under the newly enabled
 * Chromium GPU compositing on Windows: a full-window `backdrop-filter:
 * blur(30px)` plus infinite animations (the brand ember's breathing pulse and
 * the push-to-talk highlight pulse) made Chromium produce and recomposite a
 * frame on every vsync. Pausing just the ember pulse on the frozen build took
 * the process from 0.22 to 0.05 cores.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import OnboardingOverlay from './OnboardingOverlay';
import OnboardingHighlight from './OnboardingHighlight';
import '../i18n';

const onboardingCss = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'styles', 'onboarding.css'),
  'utf8',
);

const baseProps = {
  isOnboarding: true,
  phase: 'welcome',
  phaseIndex: 0,
  totalPhases: 6,
  phaseContent: 'Thank you for trying Viola.',
  isSpeaking: true,
  suggestions: null,
  micPermission: { checking: false, granted: false, blocked: false, message: null },
  cloudConsentStatus: 'idle',
  autonomyStatus: 'idle',
  attributionStatus: 'idle',
  tryCommandStatus: 'idle',
  signInStatus: 'awaiting_user',
  skipOnboarding: vi.fn(),
  onWelcomeContinue: vi.fn(),
  onOpenSettings: vi.fn(),
};

const PHASE_PROPS = {
  welcome: { isWelcomePhase: true },
  account_pair: { isAccountPairPhase: true, phaseIndex: 1 },
  cloud_consent: { isCloudConsentPhase: true, phaseIndex: 2 },
  autonomy_tier: { isAutonomyPhase: true, phaseIndex: 3 },
  mic_try: {
    isMicPermissionPhase: true,
    phaseIndex: 4,
    micPermission: { checking: true, granted: false, blocked: false, message: null },
    suggestions: [{ text: 'what is two plus two', label: 'What is two plus two' }],
  },
  hear_about: { isAttributionPhase: true, phaseIndex: 5 },
};

function inlineAnimations(root) {
  return [root, ...root.querySelectorAll('*')]
    .map((el) => el.style?.animation || el.style?.animationIterationCount || '')
    .filter(Boolean);
}

describe('onboarding overlay keeps the compositor idle', () => {
  it('has no backdrop-filter and an opaque backdrop', () => {
    render(<OnboardingOverlay {...baseProps} {...PHASE_PROPS.welcome} />);
    const backdrop = screen.getByTestId('onboarding-overlay');

    expect(backdrop.style.backdropFilter || '').toBe('');
    expect(backdrop.style.webkitBackdropFilter || backdrop.style.WebkitBackdropFilter || '').toBe('');
    expect(backdrop.getAttribute('style')).not.toMatch(/backdrop-filter/i);
    // The app behind is hidden by paint, not by a per-frame blur.
    expect(backdrop.getAttribute('style')).toMatch(/#0b0908|rgb\(11, 9, 8\)/i);
  });

  it('declares no infinite CSS animation in the onboarding stylesheet', () => {
    const declarations = onboardingCss.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(declarations).not.toMatch(/\binfinite\b/);
    expect(onboardingCss).not.toMatch(/animation:\s*violaOnboardEmberPulse/);
  });

  it.each(Object.keys(PHASE_PROPS))('renders the %s phase with no infinite inline animation', (phase) => {
    render(<OnboardingOverlay {...baseProps} phase={phase} {...PHASE_PROPS[phase]} />);
    const backdrop = screen.getByTestId('onboarding-overlay');
    for (const animation of inlineAnimations(backdrop)) {
      expect(animation).not.toMatch(/infinite/);
    }
  });

  it('stops the push-to-talk highlight pulse after a few cycles', () => {
    const target = document.createElement('button');
    target.id = 'ptt-button';
    document.body.appendChild(target);
    try {
      render(<OnboardingHighlight targetId="ptt-button" active />);
      const highlight = screen.getByTestId('onboarding-highlight');
      expect(highlight.style.animation).toMatch(/onboarding-pulse/);
      expect(highlight.style.animation).not.toMatch(/infinite/);
    } finally {
      target.remove();
    }
  });
});
