import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CallBriefing, {
  CALL_APPROVE_COMMAND,
  CALL_CANCEL_COMMAND,
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
