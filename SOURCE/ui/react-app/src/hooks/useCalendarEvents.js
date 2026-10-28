import { useState, useCallback, useRef, useEffect } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';
import {
  format,
  startOfMonth,
  endOfMonth,
  addMonths,
  subMonths,
  parseISO,
} from 'date-fns';

const CACHE_FRESH_MS = 5 * 60 * 1000;
// A tapped day re-reads its month when the copy is older than this, so an
// event added by voice or on another device a moment ago shows up.
const DAY_TAP_FRESH_MS = 30 * 1000;
const TODAY_CHECK_MS = 60 * 1000;

function localDayKey(date = new Date()) {
  return format(date, 'yyyy-MM-dd');
}

function mergedByDate(cache) {
  const merged = {};
  for (const entry of Object.values(cache)) {
    if (entry?.byDate) Object.assign(merged, entry.byDate);
  }
  return merged;
}

// The `/v1/calendar/*` routes serve local-primary CRUD everywhere, including
// the cloud SPA (backend/cloud_route_manifest.py "calendar" group, CLOUD_NATIVE
// + tenant-safety reviewed 2026-07-06); a paired multiroom spoke reaches the
// same routes through the hub. Google/Microsoft OAuth SYNC on top of that CRUD
// still requires the user's own Tier-3 tokens and stays desktop-only.

/**
 * Hook for fetching and caching calendar events by month.
 *
 * Returns events grouped by date key (YYYY-MM-DD) for O(1) day-cell lookup,
 * plus navigation helpers and loading/error state.
 */
export function useCalendarEvents() {
  // Current viewing month (only year+month matter)
  const [viewDate, setViewDate] = useState(() => new Date());
  // Today's local date, kept live. It used to be read once at mount, so an
  // app left running across days (or a sleep) showed an old week with no
  // "today" until a restart (1.0.5 build 5).
  const [todayKey, setTodayKey] = useState(() => localDayKey());
  const navigatedRef = useRef(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [calendarStatus, setCalendarStatus] = useState('unknown');

  // Cache: monthKey -> { byDate: { 'YYYY-MM-DD': event[] }, fetchedAt: number, calendarStatus: string }
  const cacheRef = useRef({});

  // Events for every month read so far, grouped by date. Merged so the week
  // strip can span two months (e.g. Nov 29 - Dec 5).
  const [eventsByDate, setEventsByDateState] = useState({});
  const publishEvents = useCallback(() => {
    setEventsByDateState(mergedByDate(cacheRef.current));
  }, []);

  const monthKey = format(viewDate, 'yyyy-MM');

  const fetchCalendarStatus = useCallback(async () => {
    if (!isFeatureAvailable('calendar')) {
      setCalendarStatus('unavailable_web');
      return 'unavailable_web';
    }
    try {
      const result = await apiFetch('/v1/calendar/status');
      const connected = result?.connected ?? result?.data?.connected;
      const nextStatus = connected === false
        ? 'not_connected'
        : connected === true ? 'connected' : 'unknown';
      setCalendarStatus(nextStatus);
      return nextStatus;
    } catch {
      setCalendarStatus('unknown');
      return 'unknown';
    }
  }, []);

  const fetchMonth = useCallback(async (date, { maxAgeMs = CACHE_FRESH_MS } = {}) => {
    if (!isFeatureAvailable('calendar')) {
      setEventsByDateState({});
      setCalendarStatus('unavailable_web');
      setLoading(false);
      setError(null);
      return {};
    }
    const key = format(date, 'yyyy-MM');
    const monthStart = startOfMonth(date);
    const monthEnd = endOfMonth(date);

    // Use cache if fresh (< 5 min by default)
    const cached = cacheRef.current[key];
    if (cached && Date.now() - cached.fetchedAt < maxAgeMs) {
      publishEvents();
      if (cached.calendarStatus) {
        setCalendarStatus(cached.calendarStatus);
      }
      setError(null);
      return cached.byDate;
    }

    setLoading(true);
    setError(null);
    try {
      const startStr = format(monthStart, 'yyyy-MM-dd');
      const endStr = format(monthEnd, 'yyyy-MM-dd');
      const result = await apiFetch(
        `/v1/calendar/events?start_date=${startStr}T00:00:00&end_date=${endStr}T23:59:59&max_results=200`
      );

      const events = result?.events || result?.data?.events || [];
      const byDate = {};

      for (const event of events) {
        let dateKey;
        if (event.start_time) {
          try {
            const parsed = typeof event.start_time === 'string'
              ? parseISO(event.start_time)
              : new Date(event.start_time);
            dateKey = format(parsed, 'yyyy-MM-dd');
          } catch {
            continue;
          }
        } else {
          continue;
        }
        if (!byDate[dateKey]) byDate[dateKey] = [];
        byDate[dateKey].push(event);
      }

      // Sort events within each day
      for (const dk of Object.keys(byDate)) {
        byDate[dk].sort((a, b) => {
          if (a.time === 'All day' && b.time !== 'All day') return -1;
          if (a.time !== 'All day' && b.time === 'All day') return 1;
          const aT = a.start_time ? new Date(a.start_time).getTime() : 0;
          const bT = b.start_time ? new Date(b.start_time).getTime() : 0;
          return aT - bT;
        });
      }

      const nextCalendarStatus = events.length > 0 ? 'connected' : await fetchCalendarStatus();
      if (events.length > 0) {
        setCalendarStatus('connected');
      }

      cacheRef.current[key] = { byDate, fetchedAt: Date.now(), calendarStatus: nextCalendarStatus };
      publishEvents();
      setLoading(false);
      return byDate;
    } catch (err) {
      console.warn('[useCalendarEvents] Fetch failed:', err.message);
      setError("Couldn't load calendar events. Check your connection and try again.");
      setLoading(false);
      // Keep stale cache if available
      if (cached) {
        publishEvents();
        if (cached.calendarStatus) {
          setCalendarStatus(cached.calendarStatus);
        }
      }
      return null;
    }
  }, [fetchCalendarStatus, publishEvents]);

  // Fetch when viewDate month changes
  useEffect(() => {
    fetchMonth(viewDate);
  }, [monthKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const navigateMonth = useCallback((direction) => {
    navigatedRef.current = true;
    setViewDate((prev) =>
      direction > 0 ? addMonths(prev, 1) : subMonths(prev, 1)
    );
  }, []);

  const goToToday = useCallback(() => {
    navigatedRef.current = false;
    setViewDate(new Date());
  }, []);

  // Make sure the month holding `day` is loaded (and recent enough).
  const ensureDay = useCallback((day, { maxAgeMs = DAY_TAP_FRESH_MS } = {}) => (
    fetchMonth(day instanceof Date ? day : new Date(day), { maxAgeMs })
  ), [fetchMonth]);

  const refetch = useCallback(() => {
    // Any month may hold the changed event; drop them all and re-read the
    // month on screen plus today's (the week strip).
    cacheRef.current = {};
    const today = new Date();
    const jobs = [fetchMonth(viewDate)];
    if (format(today, 'yyyy-MM') !== monthKey) jobs.push(fetchMonth(today));
    return Promise.all(jobs).then((results) => results[0]);
  }, [monthKey, viewDate, fetchMonth]);

  // Keep "today" true across midnight, sleep and long-running sessions.
  useEffect(() => {
    const check = () => {
      const next = localDayKey();
      setTodayKey((prev) => (prev === next ? prev : next));
    };
    const onVisible = () => {
      if (typeof document === 'undefined' || document.visibilityState !== 'hidden') check();
    };
    const timer = window.setInterval(check, TODAY_CHECK_MS);
    window.addEventListener('focus', onVisible);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', onVisible);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);

  const firstTodayRef = useRef(true);
  useEffect(() => {
    if (firstTodayRef.current) {
      firstTodayRef.current = false;
      return;
    }
    // The day changed under a running app: show today's month again unless
    // the user is browsing another month, and re-read events.
    if (!navigatedRef.current) setViewDate(new Date());
    cacheRef.current = {};
    void fetchMonth(new Date());
  }, [todayKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const handleCalendarUpdated = () => {
      void refetch();
    };

    window.addEventListener('viola:calendar-updated', handleCalendarUpdated);
    return () => {
      window.removeEventListener('viola:calendar-updated', handleCalendarUpdated);
    };
  }, [refetch]);

  return {
    viewDate,
    todayKey,
    eventsByDate,
    loading,
    error,
    calendarStatus,
    navigateMonth,
    goToToday,
    ensureDay,
    refetch,
  };
}
