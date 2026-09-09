import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import CallSummaryCard from './CallSummaryCard';

describe('CallSummaryCard', () => {
  it('renders outcome, duration, summary, and opens the transcript', async () => {
    const onDismiss = vi.fn();
    const onViewTranscript = vi.fn();
    const { user } = render(
      <CallSummaryCard
        call={{
          call_id: 'call-1',
          phone_number: '+1 555 0100',
          outcome: 'Booking confirmed',
          summary: 'Appointment set for Tuesday at 2 PM.',
          duration_seconds: 154,
        }}
        onDismiss={onDismiss}
        onViewTranscript={onViewTranscript}
      />
    );

    expect(screen.getByText('Call ended')).toBeInTheDocument();
    expect(screen.getByText('Booking confirmed')).toBeInTheDocument();
    expect(screen.getByText('Appointment set for Tuesday at 2 PM.')).toBeInTheDocument();
    expect(screen.getByText('02:34 - +1 555 0100')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /view transcript/i }));
    expect(onViewTranscript).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: /dismiss call summary/i }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
