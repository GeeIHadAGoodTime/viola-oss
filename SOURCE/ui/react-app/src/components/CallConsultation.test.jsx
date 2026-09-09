import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import { THEME } from '../config';
import CallConsultation from './CallConsultation';

describe('CallConsultation', () => {
  it('uses the phone theme and keeps reply/takeover actions button-driven', async () => {
    const onReply = vi.fn();
    const onTakeover = vi.fn();
    const onDismiss = vi.fn();
    const { user } = render(
      <CallConsultation
        consultation={{
          call_id: 'call-123',
          question: 'Should Viola confirm the held appointment slot?',
          urgency: 'high',
        }}
        onReply={onReply}
        onTakeover={onTakeover}
        onDismiss={onDismiss}
      />
    );

    const panel = screen.getByRole('dialog');
    expect(panel).toHaveStyle({
      backgroundColor: THEME.colors.bgElevated,
      position: 'relative',
    });
    expect(screen.getByText('Needs a quick answer')).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText('Type what Viola should say...'), 'Yes, confirm it.');
    await user.click(screen.getByRole('button', { name: /^send$/i }));
    expect(onReply).toHaveBeenCalledWith('call-123', 'Yes, confirm it.');

    await user.click(screen.getByRole('button', { name: /^take over$/i }));
    expect(onTakeover).toHaveBeenCalledWith('call-123');

    await user.click(screen.getByRole('button', { name: /dismiss consultation/i }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
