import PropTypes from 'prop-types';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import { THEME } from '../config';
import CallConsultation from './CallConsultation';

function ReplyHarness({ onReply, ...props }) {
  const [answer, setAnswer] = useState('');
  return <CallConsultation {...props} reply={{ answer, setAnswer, pending: false, error: '', submit: () => onReply(props.consultation.call_id, answer.trim()) }} />;
}

ReplyHarness.propTypes = {
  onReply: PropTypes.func.isRequired,
  consultation: PropTypes.shape({ call_id: PropTypes.string.isRequired }).isRequired,
};

describe('CallConsultation', () => {
  it('uses the phone theme and keeps reply/takeover actions button-driven', async () => {
    const onReply = vi.fn();
    const onTakeover = vi.fn();
    const onDismiss = vi.fn();
    const { user } = render(
      <ReplyHarness
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
