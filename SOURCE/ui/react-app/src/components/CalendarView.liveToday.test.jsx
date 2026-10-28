/**
 * Top-bar mini calendar (1.0.5 build 5 laptop report):
 * - it showed the wrong week with no "today" until a restart, because "today"
 *   was read once at mount;
 * - tapping a day with a local event created by voice showed "No events",
 *   because the tap only read a month copy nothing refreshed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '../test/test-utils';
import CalendarView from './CalendarView';
import { apiFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({ apiFetch: vi.fn() }));
vi.mock('../utils/featureSurface', () => ({ isFeatureAvailable: () => true }));

const theme = { colors: { accent: '#C89B3C', accentGlow: 'rgba(200,155,60,0.3)', textMuted: '#999', textPrimary: '#eee' } };
let events;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
  events = [];
  apiFetch.mockReset().mockImplementation(async (path) => {
    if (path.startsWith('/v1/calendar/status')) return { connected: true };
    if (path.startsWith('/v1/calendar/events')) return { events: events.slice() };
    return {};
  });
});
afterEach(() => { vi.useRealTimers(); });

const stripDays = () => Array.from(screen.getByTestId('calendar-compact').querySelectorAll('[data-testid^="calendar-day-"]'))
  .map((cell) => cell.getAttribute('data-testid').replace('calendar-day-', ''));

describe('week strip follows the real date', () => {
  it('moves to the new week and highlights today after the day changes', async () => {
    vi.setSystemTime(new Date('2026-10-24T10:00:00'));
    await act(async () => { render(<CalendarView theme={theme} compact />); });
    expect(stripDays()).toEqual(['2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24']);

    // Running across days (or asleep): four days later the clock moves on.
    vi.setSystemTime(new Date('2026-10-28T10:40:00'));
    await act(async () => { await vi.advanceTimersByTimeAsync(60 * 1000); });

    expect(stripDays()).toEqual(['2026-10-25', '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-10-31']);
    expect(screen.getByTestId('calendar-day-2026-10-28')).toHaveAttribute('aria-current', 'date');
  });

  it('shows the real week even when it spans two months', async () => {
    vi.setSystemTime(new Date('2026-12-01T09:00:00'));
    await act(async () => { render(<CalendarView theme={theme} compact />); });
    expect(stripDays()).toEqual(['2026-11-29', '2026-11-30', '2026-12-01', '2026-12-02', '2026-12-03', '2026-12-04', '2026-12-05']);
    expect(screen.getByTestId('calendar-day-2026-12-01')).toHaveAttribute('aria-current', 'date');
  });
});

describe('tapping a day lists its events', () => {
  it('re-reads the day when its copy is old, so a voice-added local event shows', async () => {
    vi.setSystemTime(new Date('2026-10-25T09:00:00'));
    await act(async () => { render(<CalendarView theme={theme} compact />); });
    fireEvent.click(screen.getByTestId('calendar-day-2026-10-26'));
    expect(screen.getByTestId('calendar-compact-events')).toHaveTextContent('No events');

    // "add Viola test tomorrow at 3pm" by voice creates a local event.
    events = [{ event_id: 'local-f21bb6d4', title: 'Viola test', start_time: '2026-10-26T15:00:00', time: '3:00 PM', source: 'local' }];
    await act(async () => { await vi.advanceTimersByTimeAsync(31 * 1000); });
    await act(async () => { fireEvent.click(screen.getByTestId('calendar-day-2026-10-26')); });

    expect(screen.getByTestId('calendar-compact-events')).toHaveTextContent('Viola test');
  });

  it('refreshes when the backend says the calendar changed', async () => {
    vi.setSystemTime(new Date('2026-10-25T09:00:00'));
    await act(async () => { render(<CalendarView theme={theme} compact />); });
    fireEvent.click(screen.getByTestId('calendar-day-2026-10-26'));
    events = [{ event_id: 'local-1', title: 'Viola test', start_time: '2026-10-26T15:00:00', time: '3:00 PM' }];

    await act(async () => { window.dispatchEvent(new Event('viola:calendar-updated')); });

    expect(screen.getByTestId('calendar-compact-events')).toHaveTextContent('Viola test');
  });
});
