import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DiagnosticsDisclosureNotice, {
  DIAGNOSTICS_CONSENT_PATH,
  shouldShowDiagnosticsDisclosure,
} from './DiagnosticsDisclosureNotice';
import { apiFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

const ARMED_UNSEEN = {
  master_baseline_enabled: true,
  disclosure_shown: false,
  baseline_opted_out: false,
};

function mockConsent(consent) {
  apiFetch.mockImplementation((path, options) => {
    if (options?.method === 'POST') return Promise.resolve({ consent: { ...consent, disclosure_shown: true } });
    return Promise.resolve({ consent });
  });
}

describe('shouldShowDiagnosticsDisclosure', () => {
  it('shows only when armed, unseen and not opted out', () => {
    expect(shouldShowDiagnosticsDisclosure(ARMED_UNSEEN)).toBe(true);
    expect(shouldShowDiagnosticsDisclosure({ ...ARMED_UNSEEN, master_baseline_enabled: false })).toBe(false);
    expect(shouldShowDiagnosticsDisclosure({ ...ARMED_UNSEEN, disclosure_shown: true })).toBe(false);
    expect(shouldShowDiagnosticsDisclosure({ ...ARMED_UNSEEN, baseline_opted_out: true })).toBe(false);
    expect(shouldShowDiagnosticsDisclosure(null)).toBe(false);
    expect(shouldShowDiagnosticsDisclosure(undefined)).toBe(false);
  });
});

describe('DiagnosticsDisclosureNotice', () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  it('discloses what is sent and what never is, with an off switch', async () => {
    mockConsent(ARMED_UNSEEN);
    render(<DiagnosticsDisclosureNotice enabled />);
    const card = await screen.findByTestId('diagnostics-disclosure');
    expect(card).toHaveTextContent('Viola sends anonymous crash reports');
    expect(card).toHaveTextContent(/never includes your name, email, account, recordings, conversations, files or passwords/i);
    expect(card).toHaveTextContent(/Settings > Account > Privacy & Data/);
    expect(screen.getByTestId('diagnostics-disclosure-turn-off')).toHaveTextContent('Turn off crash reports');
    expect(apiFetch).toHaveBeenCalledWith(DIAGNOSTICS_CONSENT_PATH);
  });

  it('OK records that the notice was shown and keeps crash reports on', async () => {
    mockConsent(ARMED_UNSEEN);
    render(<DiagnosticsDisclosureNotice enabled />);
    await userEvent.click(await screen.findByTestId('diagnostics-disclosure-ok'));
    const post = apiFetch.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(post[0]).toBe(DIAGNOSTICS_CONSENT_PATH);
    expect(JSON.parse(post[1].body)).toEqual({ disclosure_shown: true });
    await waitFor(() => expect(screen.queryByTestId('diagnostics-disclosure')).not.toBeInTheDocument());
  });

  it('Turn off opts this install out', async () => {
    mockConsent(ARMED_UNSEEN);
    render(<DiagnosticsDisclosureNotice enabled />);
    await userEvent.click(await screen.findByTestId('diagnostics-disclosure-turn-off'));
    const post = apiFetch.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(JSON.parse(post[1].body)).toEqual({ baseline_opted_out: true, disclosure_shown: true });
    await waitFor(() => expect(screen.queryByTestId('diagnostics-disclosure')).not.toBeInTheDocument());
  });

  it('stays visible with an error when saving fails', async () => {
    apiFetch.mockImplementation((path, options) => (
      options?.method === 'POST' ? Promise.reject(new Error('nope')) : Promise.resolve({ consent: ARMED_UNSEEN })
    ));
    render(<DiagnosticsDisclosureNotice enabled />);
    await userEvent.click(await screen.findByTestId('diagnostics-disclosure-ok'));
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't save that");
    expect(screen.getByTestId('diagnostics-disclosure')).toBeInTheDocument();
  });

  it('does not show once seen, when opted out, or when the feature is off', async () => {
    for (const consent of [
      { ...ARMED_UNSEEN, disclosure_shown: true },
      { ...ARMED_UNSEEN, baseline_opted_out: true },
      { ...ARMED_UNSEEN, master_baseline_enabled: false },
    ]) {
      apiFetch.mockReset();
      mockConsent(consent);
      const { unmount } = render(<DiagnosticsDisclosureNotice enabled />);
      await waitFor(() => expect(apiFetch).toHaveBeenCalled());
      expect(screen.queryByTestId('diagnostics-disclosure')).not.toBeInTheDocument();
      unmount();
    }
  });

  it('never calls the desktop route when disabled (cloud web app, spoke, onboarding)', () => {
    mockConsent(ARMED_UNSEEN);
    const { container } = render(<DiagnosticsDisclosureNotice enabled={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it('hides quietly when the consent read fails', async () => {
    apiFetch.mockRejectedValue(new Error('offline'));
    render(<DiagnosticsDisclosureNotice enabled />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(screen.queryByTestId('diagnostics-disclosure')).not.toBeInTheDocument();
  });

  it('offers a shortcut to the privacy settings', async () => {
    mockConsent(ARMED_UNSEEN);
    const onOpen = vi.fn();
    render(<DiagnosticsDisclosureNotice enabled onOpenPrivacySettings={onOpen} />);
    await userEvent.click(await screen.findByTestId('diagnostics-disclosure-settings'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
