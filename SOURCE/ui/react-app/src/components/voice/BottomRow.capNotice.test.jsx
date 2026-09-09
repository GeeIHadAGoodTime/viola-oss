/**
 * Proves the cap-denial affordance is actually wired into the response area
 * that every surface renders (desktop shell, browser /app, multiroom spoke all
 * mount this same BottomRow). Component-level coverage of UsageCapNotice on its
 * own would pass even if nothing ever rendered it (candidate C-077).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import BottomRow from './BottomRow';

const IDLE_VOICE = { error: null, isProcessing: false, isRecording: false, isBusy: false };

function renderBottomRow(overrides = {}) {
  return render(
    <BottomRow
      isTyping={false}
      typingInput=""
      setTypingInput={() => {}}
      isCommandLoading={false}
      lastResponse="You've reached your weekly managed AI limit."
      voice={IDLE_VOICE}
      wakeStatus="idle"
      handlePTTStart={() => {}}
      handlePTTEnd={() => {}}
      connected
      {...overrides}
    />,
  );
}

describe('BottomRow managed-AI cap affordance', () => {
  it('shows no upgrade action on an ordinary turn', () => {
    renderBottomRow({ lastResponse: 'Playing music.' });
    expect(screen.queryByTestId('usage-cap-notice')).not.toBeInTheDocument();
  });

  it('renders the upgrade action beside the denial text and routes the tap', async () => {
    const onUpgradeFromCap = vi.fn();
    renderBottomRow({
      capDenial: { plan: 'free', period: 'weekly', resetsAt: '2026-08-01T00:00:00+00:00' },
      onUpgradeFromCap,
    });
    expect(screen.getByText(/reached your weekly managed AI limit/)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId('usage-cap-upgrade'));
    expect(onUpgradeFromCap).toHaveBeenCalledTimes(1);
  });

  // A capped user who asks again gets "Thinking..." in place of the answer
  // text. The button must survive that, or it vanishes from under a user
  // mid-tap.
  it('keeps the upgrade action visible while a following turn is in flight', () => {
    renderBottomRow({
      isCommandLoading: true,
      capDenial: { plan: 'free', period: 'weekly', resetsAt: '' },
      onUpgradeFromCap: () => {},
    });
    expect(screen.getByTestId('usage-cap-upgrade')).toBeInTheDocument();
  });

  // Typing swaps the whole response area for the input echo, which would take
  // the route away exactly while the user is composing the message that will
  // be capped again.
  it('keeps the upgrade action visible while the user is typing', () => {
    renderBottomRow({
      isTyping: true,
      typingInput: 'what is the weather',
      capDenial: { plan: 'free', period: 'weekly', resetsAt: '' },
      onUpgradeFromCap: () => {},
    });
    expect(screen.getByTestId('usage-cap-upgrade')).toBeInTheDocument();
  });

  it('stays absent while typing on an uncapped session', () => {
    renderBottomRow({ isTyping: true, typingInput: 'play something' });
    expect(screen.queryByTestId('usage-cap-notice')).not.toBeInTheDocument();
  });
});
