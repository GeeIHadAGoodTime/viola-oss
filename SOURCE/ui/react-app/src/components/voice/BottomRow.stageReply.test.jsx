/**
 * Two stage defects from the 1.0.5 build 5 laptop report:
 *
 * 1. The reply line printed markdown literally (`**12**`, `[FIFA](https://...)`).
 * 2. On touch, the hold-to-talk button slid right under the finger: the status
 *    label under it went from "listening for wake word" to "recording", its
 *    auto-width grid column shrank, and the centred button moved ~45px. The
 *    PTT box now has a fixed width and a fixed one-line label.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import BottomRow from './BottomRow';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// CSS modules are stubbed in tests; read the stylesheet itself (cwd = ui/react-app).
const css = readFileSync(join(process.cwd(), 'src/components/voice/BottomRow.module.css'), 'utf8');

const IDLE_VOICE = { error: null, isProcessing: false, isRecording: false, isBusy: false };

function renderReply(lastResponse, voice = IDLE_VOICE) {
  return render(
    <BottomRow
      isTyping={false}
      typingInput=""
      setTypingInput={() => {}}
      isCommandLoading={false}
      lastResponse={lastResponse}
      voice={voice}
      wakeStatus="wake_listening"
      handlePTTStart={() => {}}
      handlePTTEnd={() => {}}
      connected
    />,
  );
}

describe('stage reply markdown', () => {
  it('shows bold as text and a link as its tappable label', () => {
    renderReply("You're on the **Viola Pro** plan. Fixtures are on [FIFA](https://www.fifa.com/worldcup).");
    expect(document.body.textContent).toContain("You're on the Viola Pro plan. Fixtures are on FIFA.");
    expect(document.body.textContent).not.toMatch(/\*\*|\]\(|https:\/\//);
    expect(screen.getByRole('link', { name: 'FIFA' })).toHaveAttribute('href', 'https://www.fifa.com/worldcup');
  });
});

describe('hold-to-talk button does not move while held', () => {
  const rule = (selector) => {
    const match = new RegExp(`\\n\\${selector}\\s*\\{([^}]*)\\}`).exec(css);
    return match ? match[1] : '';
  };

  it('gives the PTT box a fixed width so its grid column cannot shrink', () => {
    expect(rule('.pttContainer')).toMatch(/\bwidth:\s*\d+px/);
  });

  it('keeps the status label to one fixed-height line', () => {
    const label = rule('.pttLabel');
    expect(label).toMatch(/white-space:\s*nowrap/);
    expect(label).toMatch(/\bheight:\s*\d+px/);
    expect(label).toMatch(/text-overflow:\s*ellipsis/);
  });

  it('renders the label inside that box in both states', () => {
    const { rerender } = renderReply('');
    expect(screen.getByLabelText('Microphone status')).toHaveTextContent('listening for wake word');
    rerender(
      <BottomRow
        isTyping={false}
        typingInput=""
        setTypingInput={() => {}}
        isCommandLoading={false}
        lastResponse=""
        voice={{ ...IDLE_VOICE, isRecording: true }}
        wakeStatus="wake_listening"
        handlePTTStart={() => {}}
        handlePTTEnd={() => {}}
        connected
      />,
    );
    expect(screen.getByLabelText('Microphone status')).toHaveTextContent('recording');
  });
});
