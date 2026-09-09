import { afterEach, describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '../../test/test-utils';
import TtsStatusIndicator from './TtsStatusIndicator';

const apiFetchMock = vi.fn();

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: (...args) => apiFetchMock(...args),
}));

describe('TtsStatusIndicator', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    // #4226: `/v1/diagnostics` is LOCAL_ONLY, so on the cloud SPA this
    // indicator renders nothing and fires no request at all. These cases are
    // about the desktop behaviour, so declare the desktop surface.
    window.viola = {};
  });

  afterEach(() => {
    delete window.viola;
  });

  // Regression for #2775: a single transient /v1/diagnostics rejection (a
  // network blip) used to flip straight to the "Enhanced voice unavailable"
  // warning. It should retry once before treating it as a real fallback
  // signal, so a blip that clears on the very next attempt never shows the
  // scary copy at all.
  it('does not warn on a single transient diagnostics failure that succeeds on retry', async () => {
    apiFetchMock
      .mockRejectedValueOnce(new Error('network blip'))
      .mockResolvedValueOnce({ settings: { tts_enabled: true } });

    render(<TtsStatusIndicator />);

    await waitFor(() => expect(screen.getByText('Voice responses: Active')).toBeInTheDocument());
    expect(screen.queryByText(/Enhanced voice unavailable/i)).not.toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });

  it('warns only after BOTH the initial attempt and the retry fail', async () => {
    apiFetchMock.mockRejectedValue(new Error('down'));

    render(<TtsStatusIndicator />);

    await waitFor(() => expect(screen.getByText(/Enhanced voice unavailable/i)).toBeInTheDocument());
    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });

  it('still reports active TTS when diagnostics succeeds on the first try', async () => {
    apiFetchMock.mockResolvedValueOnce({ settings: { tts_enabled: true } });

    render(<TtsStatusIndicator />);

    await waitFor(() => expect(screen.getByText('Voice responses: Active')).toBeInTheDocument());
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
  });
});
