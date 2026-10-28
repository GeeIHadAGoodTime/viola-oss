import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import TimerCountdown from './TimerCountdown';

const ws = vi.hoisted(() => ({ handler: null }));
vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: (handler) => {
    ws.handler = handler;
    return { send: vi.fn(), connectCount: 0 };
  },
}));

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('../hooks/useViolaApi', () => ({ apiFetch: api.apiFetch }));

function routeApi({ timers = [], alarms = [] } = {}) {
  api.apiFetch.mockImplementation(async (path) => {
    if (path === '/v1/timers') return { ok: true, timers, count: timers.length };
    if (path === '/v1/alarms/ringing') return { alarms, count: alarms.length };
    if (path === '/v1/alarms/stop') return { stopped: 1 };
    throw new Error(`unexpected ${path}`);
  });
}

async function emit(type, payload) {
  await act(async () => {
    ws.handler({ type, payload });
  });
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  ws.handler = null;
});

describe('TimerCountdown alarm card (X1.3)', () => {
  it('shows a Stop card while the alarm rings and posts the stop on tap', async () => {
    routeApi();
    render(<TimerCountdown />);
    await emit('alarm_ringing', { alarm_id: '9', label: 'Alarm', message: 'Your alarm is going off.' });

    expect(screen.getByTestId('alarm-ringing-card')).toHaveTextContent('Your alarm is going off.');
    await act(async () => {
      fireEvent.click(screen.getByTestId('alarm-stop-button'));
    });

    expect(api.apiFetch).toHaveBeenCalledWith('/v1/alarms/stop', {
      method: 'POST',
      body: JSON.stringify({ alarm_id: '9' }),
    });
    expect(screen.queryByTestId('alarm-ringing-card')).not.toBeInTheDocument();
  });

  it('takes the card down when the backend says the ring ended (voice stop or timeout)', async () => {
    routeApi();
    render(<TimerCountdown />);
    await emit('alarm_ringing', { alarm_id: '12', label: 'Wake up' });
    expect(screen.getByTestId('alarm-ringing-card')).toHaveTextContent('Wake up');

    await emit('alarm_stopped', { alarm_id: '12', reason: 'timeout' });
    expect(screen.queryByTestId('alarm-ringing-card')).not.toBeInTheDocument();
  });

  it('shows an alarm that was already ringing when the page loaded', async () => {
    routeApi({ alarms: [{ alarm_id: '3', label: 'Alarm', message: 'Your alarm is going off.' }] });
    render(<TimerCountdown />);
    expect(await screen.findByTestId('alarm-ringing-card')).toBeInTheDocument();
  });
});

describe('TimerCountdown sleep timer chip (X1.3)', () => {
  it('shows a sleep timer from the snapshot with its own chip', async () => {
    routeApi();
    render(<TimerCountdown />);
    const end = new Date(Date.now() + 30 * 60 * 1000).toISOString();
    await emit('timer_update', {
      timers: [{ timer_id: 'sleep-5', label: 'Sleep timer', end_time: end, remaining_seconds: 1800, kind: 'sleep' }],
      count: 1,
    });

    const chip = screen.getByTestId('sleep-timer-chip');
    expect(chip).toHaveTextContent('Sleep timer');
    expect(chip).toHaveTextContent('30:00');
    expect(screen.getByTestId('sleep-timer-icon')).toBeInTheDocument();
    expect(screen.queryByTestId('timer-chip')).not.toBeInTheDocument();
  });

  it('seeds the sleep timer chip from /v1/timers on mount', async () => {
    const end = new Date(Date.now() + 10 * 60 * 1000).toISOString();
    routeApi({ timers: [{ timer_id: 'sleep-7', label: 'Sleep timer', end_time: end, kind: 'sleep' }] });
    render(<TimerCountdown />);
    expect(await screen.findByTestId('sleep-timer-chip')).toHaveTextContent('Sleep timer');
  });
});
