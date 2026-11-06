import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CallBriefing, {
  actionApprovalFromConsultEvent,
  callBriefingFromConsultEvent,
} from './CallBriefing';

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
