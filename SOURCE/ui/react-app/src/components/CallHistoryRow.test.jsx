/**
 * #3554: a call that never connected still shows when it happened.
 *
 * Observed live on https://api.useviola.com/app: 4 of 27 real calls in the
 * phone stage rendered "Unknown date" with no duration, including the newest
 * ones. Those records carry an empty `started_at` because
 * telephony/call_manager.py only assigns it once the Telnyx media stream
 * connects, so a no-answer / busy / rejected / failed call is persisted without
 * one. The record does know when it was placed (`created_at`) and when the
 * attempt ended (`ended_at`); this row now reads them in that order rather than
 * giving up at the first empty field.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import CallHistoryRow from './CallHistoryRow';

const renderRow = (call) => render(
  <CallHistoryRow call={call} expanded={false} onToggle={vi.fn()} />,
);

const baseCall = {
  call_id: 'call-1',
  phone_number: '+13125550100',
  status: 'no_answer',
  duration_seconds: 0,
};

describe('CallHistoryRow date (#3554)', () => {
  it('dates a never-connected call from its placed-at time', () => {
    renderRow({
      ...baseCall,
      started_at: '',
      created_at: '2026-07-24T23:24:00Z',
      ended_at: '2026-07-24T23:24:31Z',
    });

    expect(screen.queryByText('Unknown date')).toBeNull();
    expect(screen.getByText(new Date('2026-07-24T23:24:00Z').toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    }))).toBeInTheDocument();
  });

  it('falls back to the end of the attempt for records saved before placed-at existed', () => {
    renderRow({ ...baseCall, started_at: '', ended_at: '2026-07-24T23:24:31Z' });

    expect(screen.queryByText('Unknown date')).toBeNull();
    expect(screen.getByText(new Date('2026-07-24T23:24:31Z').toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    }))).toBeInTheDocument();
  });

  it('still prefers the connected-at time on a normal call', () => {
    renderRow({
      ...baseCall,
      status: 'completed',
      duration_seconds: 120,
      started_at: '2026-07-24T23:30:00Z',
      created_at: '2026-07-24T23:29:12Z',
      ended_at: '2026-07-24T23:32:00Z',
    });

    expect(screen.getByText(new Date('2026-07-24T23:30:00Z').toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    }))).toBeInTheDocument();
  });

  it('says so plainly when a record carries no timestamp at all', () => {
    renderRow({ ...baseCall, started_at: '' });

    expect(screen.getByText('Unknown date')).toBeInTheDocument();
  });

  it('ignores an unparseable timestamp and uses the next one', () => {
    renderRow({ ...baseCall, started_at: 'not-a-date', ended_at: '2026-07-24T23:24:31Z' });

    expect(screen.queryByText('Unknown date')).toBeNull();
  });
});
