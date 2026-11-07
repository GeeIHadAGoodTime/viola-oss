import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CallBriefing, {
  CALL_APPROVE_COMMAND,
  CALL_CANCEL_COMMAND,
  actionApprovalFromConsultEvent,
  answerCallBriefing,
  callBriefingBelongsElsewhere,
  callBriefingFromConsultEvent,
} from './CallBriefing';

vi.mock('../config', () => ({
  THEME: { colors: { statusGreen: '#22c55e', statusYellow: '#eab308' } },
}));

// The executor's consult_user payload for a phone dial found by web search.
const pendingSearchCall = {
  tool_name: 'phone',
  action_class: 'phone_call',
  planned_action: "call Joe's Pizza at +13125550142 (this number came from a search result or other outside content, so please check it is the right one)",
  status: 'deferred',
  phone_number: '+13125550142',
  recipient_name: "Joe's Pizza",
  objective: 'Order a large cheese pizza for pickup at 6pm',
  untrusted_source: true,
  task_id: 'task-1',
};

describe('callBriefingBelongsElsewhere', () => {
  const inChatA = { chat_thread_id: 'chat-a', awaiting_confirmation: true };

  it('hides a chat approval while a different chat is open', () => {
    expect(callBriefingBelongsElsewhere(inChatA, 'chat-b')).toBe(true);
  });

  it('shows it in its own chat and outside chat', () => {
    expect(callBriefingBelongsElsewhere(inChatA, 'chat-a')).toBe(false);
    expect(callBriefingBelongsElsewhere(inChatA, null)).toBe(false);
  });

  it('shows an approval that was not asked in a saved chat', () => {
    expect(callBriefingBelongsElsewhere({ awaiting_confirmation: true }, 'chat-b')).toBe(false);
    expect(callBriefingBelongsElsewhere(null, 'chat-b')).toBe(false);
  });
});

describe('callBriefingFromConsultEvent', () => {
  it('turns a pending phone confirmation into an awaiting briefing with the exact number', () => {
    const briefing = callBriefingFromConsultEvent(pendingSearchCall);
    expect(briefing).toMatchObject({
      phone_number: '+13125550142',
      business_name: "Joe's Pizza",
      awaiting_confirmation: true,
      approved: false,
      untrusted_source: true,
    });
  });

  it('marks an approved gate and clears on a denial', () => {
    expect(callBriefingFromConsultEvent({ ...pendingSearchCall, status: 'approved' })).toMatchObject({
      approved: true,
      awaiting_confirmation: false,
    });
    expect(callBriefingFromConsultEvent({ ...pendingSearchCall, status: 'denied' })).toBeNull();
    expect(callBriefingFromConsultEvent({ ...pendingSearchCall, status: 'unavailable' })).toBeNull();
  });

  it('ignores confirmations that are not phone calls', () => {
    expect(callBriefingFromConsultEvent({ action_class: 'send_email', status: 'pending' })).toBeUndefined();
    expect(callBriefingFromConsultEvent(null)).toBeUndefined();
  });
});

describe('CallBriefing approve and cancel', () => {
  const briefing = callBriefingFromConsultEvent(pendingSearchCall);

  it('shows who the number belongs to, the exact number, and the outside-content warning', () => {
    render(<CallBriefing briefing={briefing} onApprove={vi.fn()} onCancel={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByText('Viola needs your approval')).toBeTruthy();
    expect(screen.getByText("Joe's Pizza")).toBeTruthy();
    expect(screen.getByTestId('call-briefing-number').textContent).toBe('+13125550142');
    expect(screen.getByText(/search result or other outside content/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });

  it('Approve answers the gate once and disables both buttons while sending', async () => {
    let resolve;
    const onApprove = vi.fn(() => new Promise((r) => { resolve = r; }));
    const onCancel = vi.fn();
    render(<CallBriefing briefing={briefing} onApprove={onApprove} onCancel={onCancel} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(screen.getByRole('button', { name: 'Calling...' }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove).toHaveBeenCalledWith(briefing);
    expect(screen.getByRole('button', { name: 'Cancel' }).disabled).toBe(true);
    expect(onCancel).not.toHaveBeenCalled();
    resolve();
  });

  it('Cancel answers the gate with cancel', async () => {
    const onCancel = vi.fn(() => Promise.resolve());
    render(<CallBriefing briefing={briefing} onApprove={vi.fn()} onCancel={onCancel} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
  });

  it('re-enables the buttons when sending fails so the user can try again', async () => {
    const onApprove = vi.fn(() => Promise.reject(new Error('offline')));
    render(<CallBriefing briefing={briefing} onApprove={onApprove} onCancel={vi.fn()} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' }).disabled).toBe(false));
  });

  it('shows no buttons once the call is approved', () => {
    const approved = callBriefingFromConsultEvent({ ...pendingSearchCall, status: 'approved' });
    render(<CallBriefing briefing={approved} onApprove={vi.fn()} onCancel={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByText('Call approved')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  });

  it('sends the same words the conversational confirm-or-cancel gate accepts', () => {
    expect(CALL_APPROVE_COMMAND).toBe('confirm');
    expect(CALL_CANCEL_COMMAND).toBe('cancel');
  });
});

describe('CallBriefing on a touch screen', () => {
  it('the dismiss, approve and cancel controls grow to 44px targets on touch', () => {
    // The dismiss control was a bare "x" glyph with an 8x18px target.
    const briefing = callBriefingFromConsultEvent(pendingSearchCall);
    const onDismiss = vi.fn();
    render(<CallBriefing briefing={briefing} onApprove={vi.fn()} onCancel={vi.fn()} onDismiss={onDismiss} />);
    const dismiss = screen.getByRole('button', { name: 'Dismiss' });
    expect(dismiss.className).toContain('viola-touch-target');
    expect(dismiss.style.padding).toBe('6px 8px');
    expect(screen.getByRole('button', { name: 'Approve' }).className).toContain('viola-touch-target');
    expect(screen.getByRole('button', { name: 'Cancel' }).className).toContain('viola-touch-target');
    fireEvent.click(dismiss);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});

describe('answerCallBriefing', () => {
  // A pending confirmation belongs to the chat it was asked in (r5 Google demo:
  // a new chat silently consumed another chat's pending yes/no).
  it('answers a call asked for in a saved chat in that chat', async () => {
    const briefing = callBriefingFromConsultEvent({ ...pendingSearchCall, chat_thread_id: 'thread-A' });
    expect(briefing.chat_thread_id).toBe('thread-A');
    const sendCommand = vi.fn(() => Promise.resolve());
    const sendToChat = vi.fn(() => Promise.resolve());

    await answerCallBriefing(briefing, CALL_APPROVE_COMMAND, { sendCommand, sendToChat });

    expect(sendToChat).toHaveBeenCalledWith('thread-A', 'confirm');
    expect(sendCommand).not.toHaveBeenCalled();
  });

  it('answers everything else through the normal command path', async () => {
    const briefing = callBriefingFromConsultEvent(pendingSearchCall);
    expect(briefing.chat_thread_id).toBeNull();
    const sendCommand = vi.fn(() => Promise.resolve());
    const sendToChat = vi.fn(() => Promise.resolve());

    await answerCallBriefing(briefing, CALL_CANCEL_COMMAND, { sendCommand, sendToChat });

    expect(sendCommand).toHaveBeenCalledWith('cancel');
    expect(sendToChat).not.toHaveBeenCalled();
  });
});

const desktop = { action_class: 'desktop_action', status: 'deferred', planned_action: 'open Notepad', task_id: 't1' };

describe('actionApprovalFromConsultEvent', () => {
  it('shows a waiting desktop action', () => {
    expect(actionApprovalFromConsultEvent(desktop)).toMatchObject({
      planned_action: 'open Notepad',
      action_class: 'desktop_action',
      awaiting_confirmation: true,
    });
  });

  it('clears the card once the action is answered', () => {
    for (const status of ['approved', 'denied', 'unavailable']) {
      expect(actionApprovalFromConsultEvent({ ...desktop, status })).toBeNull();
    }
  });

  it('leaves calls and payments to their own cards', () => {
    expect(actionApprovalFromConsultEvent({ action_class: 'phone_call', status: 'pending' })).toBeUndefined();
    expect(actionApprovalFromConsultEvent({ action_class: 'payment', status: 'pending' })).toBeUndefined();
    expect(callBriefingFromConsultEvent(desktop)).toBeUndefined();
  });

  it('answers in the chat that asked', () => {
    expect(actionApprovalFromConsultEvent({ ...desktop, chat_thread_id: 'c9' }).chat_thread_id).toBe('c9');
  });
});

describe('the approval card for an action', () => {
  const briefing = actionApprovalFromConsultEvent(desktop);

  it('says what Viola wants to do, with Approve and Cancel', async () => {
    const onApprove = vi.fn().mockResolvedValue();
    const onCancel = vi.fn().mockResolvedValue();
    render(<CallBriefing briefing={briefing} onApprove={onApprove} onCancel={onCancel} onDismiss={() => {}} />);
    expect(screen.getByRole('dialog', { name: 'Action approval' })).toBeTruthy();
    expect(screen.getByTestId('action-approval-text').textContent).toBe('Viola wants to open Notepad.');
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('a call still shows its number', () => {
    render(
      <CallBriefing
        briefing={{ phone_number: '+15550100', business_name: 'Pizza', awaiting_confirmation: true }}
        onApprove={() => {}}
        onCancel={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByRole('dialog', { name: 'Phone call approval' })).toBeTruthy();
    expect(screen.getByTestId('call-briefing-number').textContent).toBe('+15550100');
  });
});
