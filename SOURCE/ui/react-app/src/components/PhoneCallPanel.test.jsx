import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import PhoneCallPanel from './PhoneCallPanel';

describe('PhoneCallPanel', () => {
  it('renders mixed final and partial transcripts and fires End call', async () => {
    const onEndCall = vi.fn();
    const onOpenHistory = vi.fn();
    const transcripts = [
      { role: 'them', text: 'Hi, I am calling about my order.', partial: false, ts: 1 },
      { role: 'viola', text: 'I can help with that.', partial: false, ts: 2 },
      { role: 'them', text: 'The order number is still coming', partial: true, ts: 3 },
    ];

    const { user } = render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{
          phone_number: '+1 555 0100',
          business_name: 'Oak Valley Veterinary',
          started_at: '2026-05-01T12:00:00Z',
          task: 'Check order status',
        }}
        onEndCall={onEndCall}
        isListening={true}
        transcripts={transcripts}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={vi.fn()}
        onOpenHistory={onOpenHistory}
      />
    );

    expect(screen.getByText('Calling +1 555 0100')).toBeInTheDocument();
    expect(screen.getByText('Check order status')).toBeInTheDocument();
    expect(screen.getByText('Hi, I am calling about my order.')).toBeInTheDocument();
    expect(screen.getByText('I can help with that.')).toBeInTheDocument();
    expect(screen.getByText('Viola')).toBeInTheDocument();
    expect(screen.getAllByText('Oak Valley Veterinary')).not.toHaveLength(0);

    // Own-side-right convention: Viola (speaking for the user) is the "own
    // side" and renders on the right; the call recipient ("them") renders on
    // the left. See PhoneCallPanel.jsx transcript row alignItems logic.
    const violaBubbleRow = screen.getByText('I can help with that.').closest('[style]').parentElement;
    expect(violaBubbleRow).toHaveStyle({ alignItems: 'flex-end' });
    const themBubbleRow = screen.getByText('Hi, I am calling about my order.').closest('[style]').parentElement;
    expect(themBubbleRow).toHaveStyle({ alignItems: 'flex-start' });

    const partialText = screen.getByText('The order number is still coming');
    expect(partialText).toBeInTheDocument();
    expect(screen.getByLabelText('Oak Valley Veterinary is live')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^end call$/i }));
    expect(onEndCall).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: /history/i }));
    expect(onOpenHistory).toHaveBeenCalledTimes(1);
  });

  it('renders Take over before Listen is active', () => {
    render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{ phone_number: '+1 555 0100' }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={vi.fn()}
      />
    );

    expect(screen.getByRole('button', { name: /take over/i })).toBeInTheDocument();
  });

  it('shows live cost and one recipient-state chip', () => {
    render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{
          phone_number: '+1 555 0100',
          current_cost_usd: 0.04,
          recipient_state: 'voicemail',
        }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={vi.fn()}
      />
    );

    expect(screen.getByTestId('phone-call-cost')).toHaveTextContent('$0.04');
    expect(screen.getByTestId('phone-recipient-state')).toHaveTextContent('Voicemail');
    expect(screen.getAllByTestId('phone-recipient-state')).toHaveLength(1);
  });

  it('sends an operator message with Enter and clears the input', async () => {
    const onSendOperatorMessage = vi.fn().mockResolvedValue({ ok: true });
    const { user } = render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{ phone_number: '+1 555 0100' }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={onSendOperatorMessage}
      />
    );

    const input = screen.getByTestId('phone-call-operator-input');
    await user.type(input, 'Insurance is BlueCross PPO BC-77419823');
    await user.keyboard('{Enter}');

    expect(onSendOperatorMessage).toHaveBeenCalledWith('Insurance is BlueCross PPO BC-77419823');
    expect(input).toHaveValue('');
  });

  it('submits suggested action chips and leaves the action in the input', async () => {
    const onSendOperatorMessage = vi.fn().mockResolvedValue({ ok: true });
    const { user } = render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{ phone_number: '+1 555 0100' }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={onSendOperatorMessage}
      />
    );

    await user.click(screen.getByRole('button', { name: /refuse upsell/i }));

    expect(onSendOperatorMessage).toHaveBeenCalledWith('Refuse upsell');
    expect(screen.getByTestId('phone-call-operator-input')).toHaveValue('Refuse upsell');
  });

  it('keeps Shift+Enter as a newline in the operator input', async () => {
    const onSendOperatorMessage = vi.fn();
    const { user } = render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{ phone_number: '+1 555 0100' }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={onSendOperatorMessage}
      />
    );

    const input = screen.getByTestId('phone-call-operator-input');
    await user.type(input, 'Line one');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(input, 'Line two');

    expect(input).toHaveValue('Line one\nLine two');
    expect(onSendOperatorMessage).not.toHaveBeenCalled();
  });

  it('renders and replies to inline consultations', async () => {
    const onConsultationReply = vi.fn();
    const onConsultationTakeover = vi.fn();
    const { user } = render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{ phone_number: '+1 555 0100' }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={vi.fn()}
        activeConsultation={{ call_id: 'call-123', question: 'Approve the alternate appointment?' }}
        onConsultationReply={onConsultationReply}
        onConsultationTakeover={onConsultationTakeover}
      />
    );

    expect(screen.getByTestId('phone-call-consult-inline')).toBeInTheDocument();
    expect(screen.getByText('Approve the alternate appointment?')).toBeInTheDocument();

    await user.type(screen.getByTestId('phone-call-consult-input'), 'Yes, accept it.');
    await user.click(screen.getByRole('button', { name: /reply/i }));

    expect(onConsultationReply).toHaveBeenCalledWith('call-123', 'Yes, accept it.');

    await user.click(screen.getAllByRole('button', { name: /^take over$/i })[0]);
    expect(onConsultationTakeover).toHaveBeenCalledWith('call-123');
  });

  it('renders queued calls and removes by position', async () => {
    const onRemoveQueuedCall = vi.fn();
    const { user } = render(
      <PhoneCallPanel
        callId="call-123"
        callMeta={{ phone_number: '+1 555 0100' }}
        onEndCall={vi.fn()}
        isListening={false}
        transcripts={[]}
        takeoverActive={false}
        onToggleListen={vi.fn()}
        onToggleTakeover={vi.fn()}
        onActivateTakeover={vi.fn()}
        onSendOperatorMessage={vi.fn()}
        queuedCalls={[
          { queue_id: 'queue-1', position: 1, phone_number: '+1 555 0200', task: 'Call pharmacy' },
          { queue_id: 'queue-2', position: 2, phone_number: '+1 555 0300', task: 'Call dentist' },
        ]}
        onRemoveQueuedCall={onRemoveQueuedCall}
      />
    );

    expect(screen.getByTestId('phone-live-badge')).toHaveTextContent('LIVE');
    expect(screen.getByTestId('phone-call-queue')).toHaveTextContent('QUEUED');
    expect(screen.getByText('+1 555 0200')).toBeInTheDocument();
    expect(screen.getByText(/Call dentist/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /remove queued call 1/i }));
    expect(onRemoveQueuedCall).toHaveBeenCalledWith(1);
  });
});
