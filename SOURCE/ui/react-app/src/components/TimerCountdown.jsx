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
// Robust across a webview reload: it also fetches /v1/timers on mount so an
// already-running timer shows immediately, before the next WS snapshot arrives.
// =========================================================================

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
  return {
    timer_id: String(raw.timer_id),
    label: raw.label || 'Timer',
    endMs,
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

export default function TimerCountdown({ addToast }) {
  const [timers, setTimers] = useState([]);
  // Force a re-render each tick so the countdown updates without churning state.
  const [, setTick] = useState(0);
  const firedRef = useRef(new Set());

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
      applySnapshot(msg.payload.timers);
    } else if (msg.type === 'timer_completed' && msg.payload) {
      const timerId = String(msg.payload.timer_id || '');
      // Guard against a duplicated completion event double-toasting.
      if (timerId && firedRef.current.has(timerId)) return;
      if (timerId) firedRef.current.add(timerId);
      const message = msg.payload.message
        || (msg.payload.label ? `Your ${msg.payload.label} is done!` : 'Your timer is done!');
      if (addToast) addToast({ message, level: 'info', persist: true });
      setTimers((prev) => prev.filter((t) => t.timer_id !== timerId));
    }
  }, [applySnapshot, addToast]);

  useWebSocket(onMessage);

  // Seed from the REST snapshot on mount so an already-running timer shows
  // even if the webview connected after the timer was set.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch('/v1/timers');
        if (!cancelled && data && Array.isArray(data.timers)) {
          applySnapshot(data.timers);
        }
      } catch {
        // Best-effort seed; the WS snapshot is the live source of truth.
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

  if (timers.length === 0) return null;

  const now = Date.now();
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
        gap: '8px',
        zIndex: 9998,
        pointerEvents: 'none',
      }}
    >
      {timers
        .slice()
        .sort((a, b) => a.endMs - b.endMs)
        .map((t) => {
          const remaining = (t.endMs - now) / 1000;
          return (
            <div
              key={t.timer_id}
              data-testid="timer-chip"
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
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={THEME.colors.accent} strokeWidth="2" strokeLinecap="round">
                <circle cx="12" cy="13" r="8" />
                <line x1="12" y1="13" x2="12" y2="9" />
                <line x1="9" y1="2" x2="15" y2="2" />
              </svg>
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
