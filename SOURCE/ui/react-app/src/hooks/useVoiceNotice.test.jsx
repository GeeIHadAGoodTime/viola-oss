import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import BottomRow from '../components/voice/BottomRow';
import { VOICE_NOTICE_TTL_MS } from './useVoiceNotice';

const HEARD_NOTHING = "Viola didn't hear anything from the microphone.";

function Row({ voice, lastResponse = '"Earlier reply"', isCommandLoading = false }) {
  return (
    <BottomRow
      isTyping={false}
      typingInput=""
      setTypingInput={() => {}}
      isCommandLoading={isCommandLoading}
      lastResponse={lastResponse}
      voice={{ isProcessing: false, isRecording: false, isBusy: false, ...voice }}
      wakeStatus="idle"
      handlePTTStart={() => {}}
      handlePTTEnd={() => {}}
      connected
    />
  );
}

describe('the "didn\'t hear anything" notice (build 9, High 1)', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('never hides the reply line', () => {
    render(<Row voice={{ error: HEARD_NOTHING }} />);
    expect(screen.getByText(HEARD_NOTHING)).toBeInTheDocument();
    expect(screen.getByText(/Earlier reply/)).toBeInTheDocument();
  });

  it('clears when the next reply lands, with voice.error still set', () => {
    const clearError = vi.fn();
    const voice = { error: HEARD_NOTHING, clearError };
    const view = render(<Row voice={voice} />);
    view.rerender(<Row voice={voice} lastResponse='"Canberra"' />);
    expect(screen.queryByText(HEARD_NOTHING)).not.toBeInTheDocument();
    expect(screen.getByText(/Canberra/)).toBeInTheDocument();
    expect(clearError).toHaveBeenCalled();
  });

  it('clears when the next turn starts, typed or spoken', () => {
    const voice = { error: HEARD_NOTHING, clearError: vi.fn() };
    const typed = render(<Row voice={voice} />);
    typed.rerender(<Row voice={voice} isCommandLoading />);
    expect(screen.queryByText(HEARD_NOTHING)).not.toBeInTheDocument();
    typed.unmount();

    const spoken = render(<Row voice={voice} />);
    spoken.rerender(<Row voice={{ ...voice, isRecording: true }} />);
    expect(screen.queryByText(HEARD_NOTHING)).not.toBeInTheDocument();
  });

  it('clears by itself on a timeout', () => {
    const clearError = vi.fn();
    render(<Row voice={{ error: HEARD_NOTHING, clearError }} />);
    act(() => { vi.advanceTimersByTime(VOICE_NOTICE_TTL_MS + 1); });
    expect(screen.queryByText(HEARD_NOTHING)).not.toBeInTheDocument();
    expect(clearError).toHaveBeenCalledTimes(1);
  });

  it('shows again for a new failure', () => {
    const view = render(<Row voice={{ error: HEARD_NOTHING }} />);
    act(() => { vi.advanceTimersByTime(VOICE_NOTICE_TTL_MS + 1); });
    view.rerender(<Row voice={{ error: null }} />);
    view.rerender(<Row voice={{ error: HEARD_NOTHING }} />);
    expect(screen.getByText(HEARD_NOTHING)).toBeInTheDocument();
  });
});
