/**
 * The stage reply line while a turn runs. Build 9: tokens were set but the
 * "processing" spinner stayed in their place until the turn ended, so the text
 * appeared all at once (T9), and the model's reasoning sat above the reply.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import BottomRow from './BottomRow';

const IDLE_VOICE = { error: null, isProcessing: false, isRecording: false, isBusy: false };

function renderBottomRow(overrides = {}) {
  return render(
    <BottomRow
      isTyping={false}
      typingInput=""
      setTypingInput={() => {}}
      isCommandLoading={false}
      lastResponse="Earlier reply."
      voice={IDLE_VOICE}
      wakeStatus="idle"
      handlePTTStart={() => {}}
      handlePTTEnd={() => {}}
      connected
      {...overrides}
    />,
  );
}

describe('BottomRow live reply', () => {
  it('shows the answer as it is written while the command is still loading', () => {
    renderBottomRow({ isCommandLoading: true, liveReply: 'A violin makes' });
    expect(screen.getByTestId('live-reply')).toHaveTextContent('A violin makes');
    expect(screen.queryByText('Earlier reply.')).not.toBeInTheDocument();
  });

  it('shows the answer as it is written during a voice turn', () => {
    renderBottomRow({ voice: { ...IDLE_VOICE, isProcessing: true }, liveReply: 'A violin makes' });
    expect(screen.getByTestId('live-reply')).toHaveTextContent('A violin makes');
  });

  it('keeps the working state until the first words arrive', () => {
    renderBottomRow({ isCommandLoading: true });
    expect(screen.queryByTestId('live-reply')).not.toBeInTheDocument();
  });

  it('has nowhere to put model reasoning', () => {
    renderBottomRow({ mainThinking: 'I should use the file_bug_report tool at 123 Main Street' });
    expect(screen.queryByText(/file_bug_report/)).not.toBeInTheDocument();
  });
});
