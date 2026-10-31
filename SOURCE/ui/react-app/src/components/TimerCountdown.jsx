import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { useWebSocket } from '../hooks/useWebSocket';
import { apiFetch } from '../hooks/useViolaApi';

// =========================================================================
// TimerCountdown (#1404)
//
// The in-app surface for timers on the desktop launch app: a live countdown
// while any timer runs, and an on-screen notification when one expires. Before
// this existed a timer set by voice/command expired with nothing shown -- the
// only signal was a one-shot TTS utterance (issue #1404). The backend
// TimerNotifier (services/notifications/timer_notifier.py) broadcasts
// `timer_update` (active snapshot) and `timer_completed` (expiry) over the same
// EventHub /ws/events socket the rest of the UI already uses.
//
// Sleep timers ride in the same snapshot with kind "sleep" and get a moon chip
// (1.0.5 laptop report X1.3: a sleep timer left nothing on screen).
//
// A ringing alarm (services/alarm_ringer.py) shows a card with a Stop button
// while it rings: `alarm_ringing` puts it up, `alarm_stopped` takes it down
// (whether the user tapped Stop, said "stop", or the ring timed out).
//
// Robust across a webview reload: it also fetches /v1/timers and
// /v1/alarms/ringing on mount so an already-running timer or ringing alarm
// shows immediately, before the next WS event arrives.
// =========================================================================

// How long the "Your timer is done!" toast stays on screen.
export const TIMER_DONE_TOAST_MS = 30000;

function normalizeTimer(raw) {
  if (!raw || !raw.timer_id) return null;
  let endMs = null;
  if (raw.end_time) {
    const parsed = Date.parse(raw.end_time);
    if (!Number.isNaN(parsed)) endMs = parsed;
  }
  if (endMs === null && typeof raw.remaining_seconds === 'number') {
    endMs = Date.now() + raw.remaining_seconds * 1000;
  }
  if (endMs === null) return null;
  const kind = raw.kind === 'sleep' ? 'sleep' : 'timer';
  return {
    timer_id: String(raw.timer_id),
    label: raw.label || (kind === 'sleep' ? 'Sleep timer' : 'Timer'),
    endMs,
    kind,
  };
}

function normalizeAlarm(raw) {
  if (!raw || raw.alarm_id === undefined || raw.alarm_id === null || raw.alarm_id === '') return null;
  return {
    alarm_id: String(raw.alarm_id),
    label: raw.label || 'Alarm',
    message: raw.message || 'Your alarm is going off.',
  };
}

function formatRemaining(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function TimerIcon({ kind }) {
  if (kind === 'sleep') {
    // Moon: when a sleep timer runs out, the music stops.
    return (
      <svg data-testid="sleep-timer-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={THEME.colors.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={THEME.colors.accent} strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="13" r="8" />
      <line x1="12" y1="13" x2="12" y2="9" />
      <line x1="9" y1="2" x2="15" y2="2" />
    </svg>
  );
}

TimerIcon.propTypes = {
  kind: PropTypes.string,
};

export default function TimerCountdown({ addToast }) {
  const [timers, setTimers] = useState([]);
  const [alarms, setAlarms] = useState([]);
  const [stopping, setStopping] = useState(false);
  // Force a re-render each tick so the countdown updates without churning state.
  const [, setTick] = useState(0);
  const firedRef = useRef(new Set());
  // Set once any live socket event lands; a slower REST seed must not then
  // overwrite the fresher state (e.g. clear a card that just started ringing).
  const liveTimersRef = useRef(false);
  const liveAlarmsRef = useRef(false);

  const applySnapshot = useCallback((list) => {
    const normalized = (Array.isArray(list) ? list : [])
      .map(normalizeTimer)
      .filter(Boolean)
      .filter((t) => t.endMs - Date.now() > -1000);
    setTimers(normalized);
  }, []);

  const onMessage = useCallback((msg) => {
    if (!msg || !msg.type) return;
    if (msg.type === 'timer_update' && msg.payload) {
      liveTimersRef.current = true;
      applySnapshot(msg.payload.timers);
    } else if (msg.type === 'timer_completed' && msg.payload) {
      const timerId = String(msg.payload.timer_id || '');
      // Guard against a duplicated completion event double-toasting.
      if (timerId && firedRef.current.has(timerId)) return;
      if (timerId) firedRef.current.add(timerId);
      const message = msg.payload.message
        || (msg.payload.label ? `Your ${msg.payload.label} is done!` : 'Your timer is done!');
      // Leaves on its own: it stayed up for minutes when it persisted (1.0.5
      // build-7 laptop report). The timer also rings and speaks.
      if (addToast) addToast({ message, level: 'info', durationMs: TIMER_DONE_TOAST_MS });
      setTimers((prev) => prev.filter((t) => t.timer_id !== timerId));
    } else if (msg.type === 'alarm_ringing' && msg.payload) {
      liveAlarmsRef.current = true;
      const alarm = normalizeAlarm(msg.payload);
      // One ring per user at a time; a new alarm replaces the old card.
      if (alarm) setAlarms([alarm]);
    } else if (msg.type === 'alarm_stopped' && msg.payload) {
      liveAlarmsRef.current = true;
      const alarmId = String(msg.payload.alarm_id ?? '');
      setAlarms((prev) => prev.filter((a) => a.alarm_id !== alarmId));
      setStopping(false);
    }
  }, [applySnapshot, addToast]);

  useWebSocket(onMessage);

  const stopAlarm = useCallback(async (alarmId) => {
    setStopping(true);
    try {
      await apiFetch('/v1/alarms/stop', { method: 'POST', body: JSON.stringify({ alarm_id: alarmId }) });
      setAlarms((prev) => prev.filter((a) => a.alarm_id !== alarmId));
    } catch {
      // Leave the card up so the user can tap again or say "stop".
    } finally {
      setStopping(false);
    }
  }, []);

  // Seed from REST on mount so an already-running timer or ringing alarm shows
  // even if the webview connected after it started.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch('/v1/timers');
        if (!cancelled && !liveTimersRef.current && data && Array.isArray(data.timers)) {
          applySnapshot(data.timers);
        }
      } catch {
        // Best-effort seed; the WS snapshot is the live source of truth.
      }
      try {
        const ringing = await apiFetch('/v1/alarms/ringing');
        if (!cancelled && !liveAlarmsRef.current && ringing && Array.isArray(ringing.alarms)) {
          setAlarms(ringing.alarms.map(normalizeAlarm).filter(Boolean));
        }
      } catch {
        // Best-effort seed; alarm_ringing over the socket is the live source.
      }
    })();
    return () => { cancelled = true; };
  }, [applySnapshot]);

  // Tick once a second while any timer is active so the display counts down.
  useEffect(() => {
    if (timers.length === 0) return undefined;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [timers.length]);

  const now = Date.now();
  // A chip whose time is up leaves on its own: a sleep timer has no completion
  // event (it just stops the music), so the clock decides.
  const visibleTimers = timers.filter((t) => t.endMs - now > -1000);
  if (visibleTimers.length === 0 && alarms.length === 0) return null;

  return (
    <div
      data-testid="timer-countdown"
      style={{
        position: 'fixed',
        top: '18px',
        left: '50%',
        transform: 'translateX(-50%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '8px',
        zIndex: 9998,
        pointerEvents: 'none',
      }}
    >
      {alarms.map((a) => (
        <div
          key={`alarm-${a.alarm_id}`}
          data-testid="alarm-ringing-card"
          role="alertdialog"
          aria-label={a.message}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '14px',
            padding: '14px 18px',
            backgroundColor: THEME.colors.bgElevated,
            borderRadius: '16px',
            border: `2px solid ${THEME.colors.bronze || THEME.colors.accent}`,
            boxShadow: `0 10px 32px ${THEME.colors.shadowHeavy}`,
            color: THEME.colors.textPrimary || THEME.colors.textSecondary,
            fontSize: '17px',
            pointerEvents: 'auto',
          }}
        >
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke={THEME.colors.bronze || THEME.colors.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
            <path d="M13.7 21a2 2 0 0 1-3.4 0" />
          </svg>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
            <span style={{ fontWeight: 600 }}>{a.label}</span>
            <span style={{ fontSize: '13px', color: THEME.colors.textSecondary }}>
              {a.message} Say &quot;stop&quot; or tap Stop.
            </span>
          </div>
          <button
            type="button"
            data-testid="alarm-stop-button"
            onClick={() => stopAlarm(a.alarm_id)}
            disabled={stopping}
            style={{
              minWidth: '88px',
              minHeight: '44px',
              padding: '0 20px',
              borderRadius: '999px',
              border: 'none',
              backgroundColor: THEME.colors.accent,
              color: '#ffffff',
              fontSize: '16px',
              fontWeight: 700,
              cursor: stopping ? 'default' : 'pointer',
            }}
          >
            Stop
          </button>
        </div>
      ))}
      {visibleTimers
        .slice()
        .sort((a, b) => a.endMs - b.endMs)
        .map((t) => {
          const remaining = (t.endMs - now) / 1000;
          return (
            <div
              key={t.timer_id}
              data-testid={t.kind === 'sleep' ? 'sleep-timer-chip' : 'timer-chip'}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '8px 16px',
                backgroundColor: `${THEME.colors.bgElevated}F0`,
                borderRadius: '999px',
                border: `1px solid ${THEME.colors.accent}`,
                boxShadow: `0 6px 24px ${THEME.colors.shadowHeavy}`,
                color: THEME.colors.textSecondary,
                fontSize: '15px',
                fontVariantNumeric: 'tabular-nums',
                pointerEvents: 'auto',
              }}
            >
              <TimerIcon kind={t.kind} />
              <span style={{ opacity: 0.85 }}>{t.label}</span>
              <span style={{ fontWeight: 600, color: THEME.colors.textPrimary || THEME.colors.textSecondary }}>
                {formatRemaining(remaining)}
              </span>
            </div>
          );
        })}
    </div>
  );
}

TimerCountdown.propTypes = {
  addToast: PropTypes.func,
};
