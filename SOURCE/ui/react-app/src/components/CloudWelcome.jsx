import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import Modal, { primaryButtonStyle, secondaryButtonStyle } from './Modal';
import { THEME } from '../config';

// =========================================================================
// CloudWelcome — first-visit welcome for the cloud/browser surface (#2607).
//
// The browser is the funnel to the desktop app, not the launch surface
// (ADR-0001, CLAUDE.md "The launch surface is the desktop app"). This is a
// short, honest orientation: what Viola does, where you are, and a pointer
// to the desktop app for the capabilities this browser tab doesn't have
// (background agents, multi-room speakers, linking accounts) -- the same
// capability list DesktopUpsell.jsx already shows per-feature. It never
// claims the browser can't do things it can.
//
// Completion is tracked server-side (useCloudWelcome -> /v1/cloud-welcome/*,
// per-user, RLS-scoped) so this never re-shows once finished or skipped.
// =========================================================================

const STEPS = [
  {
    title: 'Welcome to Viola',
    body: "Talk to Viola like you would a person. Ask it to look something up, set a reminder, or just have a conversation. Type below, or use your microphone.",
  },
  {
    title: "You're using Viola in your browser",
    body: 'This is Viola running right in your browser, no install needed. Most of what Viola can do works here.',
  },
  {
    title: 'Get the desktop app',
    body: 'For background agents, multi-room speakers, and linking accounts like Google Calendar or Spotify, download Viola for your desktop. Sign in with the same account and everything here carries over.',
    cta: { label: 'Download desktop app', href: 'https://useviola.com/download' },
  },
];

export default function CloudWelcome({ isOpen, saving, onFinish, onSkip }) {
  const [stepIndex, setStepIndex] = useState(0);

  // Start at the first step every time the welcome opens, so a re-open
  // (e.g. a dev hot-reload with the same mounted instance) never resumes
  // mid-tour.
  useEffect(() => {
    if (isOpen) setStepIndex(0);
  }, [isOpen]);

  const step = STEPS[stepIndex];
  const isLastStep = stepIndex === STEPS.length - 1;

  const handleNext = useCallback(() => {
    if (isLastStep) {
      onFinish();
      return;
    }
    setStepIndex((index) => Math.min(index + 1, STEPS.length - 1));
  }, [isLastStep, onFinish]);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onSkip}
      title={step.title}
      footer={(
        <>
          <button type="button" style={secondaryButtonStyle} onClick={onSkip} disabled={saving}>
            Skip
          </button>
          <button type="button" style={primaryButtonStyle} onClick={handleNext} disabled={saving}>
            {isLastStep ? (saving ? 'Finishing...' : 'Got it') : 'Next'}
          </button>
        </>
      )}
    >
      <div style={{ color: THEME.colors.textSecondary, fontSize: '15px', lineHeight: 1.6 }}>
        <p style={{ marginTop: 0 }}>{step.body}</p>
        {step.cta ? (
          <a
            href={step.cta.href}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '4px',
              color: THEME.colors.accent,
              fontWeight: 600,
              textDecoration: 'none',
            }}
          >
            {step.cta.label}
            <span aria-hidden="true">&rarr;</span>
          </a>
        ) : null}
        <div
          role="progressbar"
          aria-label="Welcome step"
          aria-valuemin={1}
          aria-valuemax={STEPS.length}
          aria-valuenow={stepIndex + 1}
          style={{ display: 'flex', gap: '6px', marginTop: '18px' }}
        >
          {STEPS.map((welcomeStep, index) => (
            <span
              key={welcomeStep.title}
              style={{
                width: '8px',
                height: '8px',
                borderRadius: '50%',
                backgroundColor: index === stepIndex ? THEME.colors.accent : THEME.colors.borderLight,
              }}
            />
          ))}
        </div>
      </div>
    </Modal>
  );
}

CloudWelcome.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  saving: PropTypes.bool,
  onFinish: PropTypes.func.isRequired,
  onSkip: PropTypes.func.isRequired,
};
