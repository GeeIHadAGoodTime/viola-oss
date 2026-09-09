/* eslint react/prop-types: "off" */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

const spokeHarness = vi.hoisted(() => ({
  calls: [],
}));

vi.mock('./components/SpokeWrapper', () => ({
  default: (props) => {
    spokeHarness.calls.push(props);
    return <div data-testid="spoke-wrapper">room:{props.room}</div>;
  },
}));

vi.mock('./SmartDisplay', () => ({
  default: () => <div data-testid="smart-display">SmartDisplay</div>,
}));

vi.mock('./components/ReviewPage', () => ({
  default: () => <div data-testid="review-page">Review</div>,
}));

vi.mock('./components/auth', () => ({
  CloudAuthGate: ({ children }) => <div data-testid="cloud-auth-gate">{children}</div>,
}));

vi.mock('./auth/AuthProvider', () => ({
  AuthProvider: ({ children }) => <>{children}</>,
}));

vi.mock('./auth/useAuth', () => ({
  useAuth: () => ({ user: null }),
}));

vi.mock('./hooks/useAuth', () => ({
  AuthProvider: ({ children }) => <>{children}</>,
  useAuth: () => ({ user: null }),
}));

vi.mock('./sentryClient', () => ({
  syncSentryUser: vi.fn(),
}));

async function renderAppAt(path) {
  vi.resetModules();
  spokeHarness.calls = [];
  window.history.replaceState({}, '', path);
  const { default: App } = await import('./App');
  render(<App />);
}

describe('App spoke routing', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/');
    delete window.viola;
  });

  afterEach(() => {
    cleanup();
    window.history.replaceState({}, '', '/');
    delete window.viola;
  });

  it('routes token-only spoke links through SpokeWrapper', async () => {
    await renderAppAt('/?spoke_token=qr-token');

    expect(screen.getByTestId('spoke-wrapper')).toHaveTextContent('room:speaker');
    expect(spokeHarness.calls[0]).toMatchObject({ room: 'speaker' });
    expect(screen.queryByTestId('cloud-auth-gate')).not.toBeInTheDocument();
  });
});
