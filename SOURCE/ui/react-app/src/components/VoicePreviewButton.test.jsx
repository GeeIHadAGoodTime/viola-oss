import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.fn();
vi.mock('../hooks/useViolaApi', () => ({ apiFetch: (...args) => apiFetch(...args) }));

import VoicePreviewButton from './VoicePreviewButton';

describe('VoicePreviewButton', () => {
  beforeEach(() => apiFetch.mockReset());

  it('previews the voice, speed and volume on screen, not the saved ones', async () => {
    apiFetch.mockResolvedValue({ data: { played: true } });
    render(<VoicePreviewButton voice="af_nova" language="en-us" rate={180} volume={0.4} />);
    await userEvent.click(screen.getByTestId('voice-preview-button'));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
    const [path, init] = apiFetch.mock.calls[0];
    expect(path).toBe('/v1/voice/preview');
    expect(JSON.parse(init.body)).toEqual({ voice: 'af_nova', language: 'en-us', rate: 180, volume: 0.4 });
    await waitFor(() => expect(screen.getByTestId('voice-preview-button')).toHaveTextContent('Play preview'));
  });

  it('says so when the voice could not be played', async () => {
    apiFetch.mockResolvedValue({ data: { played: false } });
    render(<VoicePreviewButton voice="af_nova" />);
    await userEvent.click(screen.getByTestId('voice-preview-button'));
    expect(await screen.findByRole('status')).toHaveTextContent('could not play');
  });
});
