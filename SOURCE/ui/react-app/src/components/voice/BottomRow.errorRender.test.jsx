/**
 * The response area is the ONLY place a failed voice turn speaks to the user,
 * so it must survive whatever the failure hands it.
 *
 * The API's failure envelope carries a structured error object
 * (`{code, message}` — contracts/api_response.py), and useVoice used to push
 * that object straight into error state. React refuses to render an object as
 * a child ("Objects are not valid as a React child"), the surrounding
 * ErrorBoundary caught the throw, and the user read "AI Response couldn't
 * load" instead of the reason their turn failed. That fired on the two
 * failures a first-run user is most likely to hit: no_speech_detected and
 * transcription_failed.
 *
 * These tests pin the render layer itself, independent of who feeds it: no
 * error value of any shape may crash this widget or print machine text.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ERROR_CODE_MESSAGES } from '../../utils/describeError';
import BottomRow from './BottomRow';

const IDLE_VOICE = { error: null, isProcessing: false, isRecording: false, isBusy: false };

function renderWithVoiceError(error) {
  return render(
    <BottomRow
      isTyping={false}
      typingInput=""
      setTypingInput={() => {}}
      isCommandLoading={false}
      lastResponse=""
      voice={{ ...IDLE_VOICE, error }}
      wakeStatus="idle"
      handlePTTStart={() => {}}
      handlePTTEnd={() => {}}
      connected
    />,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('BottomRow voice error rendering', () => {
  it('renders a plain string error as-is', () => {
    renderWithVoiceError('No microphone found');
    expect(screen.getByText('No microphone found')).toBeInTheDocument();
    expect(screen.queryByText(/couldn't load/)).not.toBeInTheDocument();
  });

  // The pre-fix crash, exactly as shipped: the envelope object reaches the
  // response area and React throws rendering it.
  it('renders the human message when handed a structured error envelope', () => {
    const crashed = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderWithVoiceError({
      code: 'no_speech_detected',
      message: 'No speech was detected in the audio.',
    });

    expect(screen.getByText(ERROR_CODE_MESSAGES.no_speech_detected)).toBeInTheDocument();
    // The ErrorBoundary fallback is what the user saw before the fix.
    expect(screen.queryByText(/couldn't load/)).not.toBeInTheDocument();
    expect(crashed).not.toHaveBeenCalled();
  });

  // A code we have no curated wording for must still show the backend's own
  // sentence, not a generic shrug — otherwise specific detail like a size
  // limit would be thrown away.
  it('falls through to the backend sentence for an unmapped code', () => {
    renderWithVoiceError({
      code: 'file_too_large',
      message: 'File too large. Maximum size: 10MB',
    });
    expect(screen.getByText('File too large. Maximum size: 10MB')).toBeInTheDocument();
  });

  it('survives an envelope carrying only a code', () => {
    const crashed = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderWithVoiceError({ code: 'transcription_failed' });

    expect(screen.queryByText(/couldn't load/)).not.toBeInTheDocument();
    expect(crashed).not.toHaveBeenCalled();
    // Never leak the raw machine code or an object stringification.
    expect(screen.queryByText(/transcription_failed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it('survives an Error instance', () => {
    const crashed = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderWithVoiceError(new Error('Failed to fetch'));

    expect(screen.queryByText(/couldn't load/)).not.toBeInTheDocument();
    expect(crashed).not.toHaveBeenCalled();
  });

  // A shape nobody anticipated must still degrade to a sentence, never to a
  // crashed widget — that is the whole point of pinning the render layer.
  it('survives an arbitrary unanticipated shape', () => {
    const crashed = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderWithVoiceError({ nested: { deeply: [1, 2, 3] } });

    expect(screen.queryByText(/couldn't load/)).not.toBeInTheDocument();
    expect(crashed).not.toHaveBeenCalled();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it('shows the ordinary response when there is no error', () => {
    render(
      <BottomRow
        isTyping={false}
        typingInput=""
        setTypingInput={() => {}}
        isCommandLoading={false}
        lastResponse="Playing music."
        voice={IDLE_VOICE}
        wakeStatus="idle"
        handlePTTStart={() => {}}
        handlePTTEnd={() => {}}
        connected
      />,
    );
    expect(screen.getByText('Playing music.')).toBeInTheDocument();
  });
});
