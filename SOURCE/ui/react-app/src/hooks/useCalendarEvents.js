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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [calendarStatus, setCalendarStatus] = useState('unknown');

  // Cache: monthKey -> { byDate: { 'YYYY-MM-DD': event[] }, fetchedAt: number, calendarStatus: string }
  const cacheRef = useRef({});

  // Events for the current month, grouped by date
  const [eventsByDate, setEventsByDate] = useState({});

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

  const fetchMonth = useCallback(async (date) => {
    if (!isFeatureAvailable('calendar')) {
      setEventsByDate({});
      setCalendarStatus('unavailable_web');
      setLoading(false);
      setError(null);
      return {};
    }
    const key = format(date, 'yyyy-MM');
    const monthStart = startOfMonth(date);
    const monthEnd = endOfMonth(date);

    // Use cache if fresh (< 5 min)
    const cached = cacheRef.current[key];
    if (cached && Date.now() - cached.fetchedAt < 5 * 60 * 1000) {
      setEventsByDate(cached.byDate);
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
      setEventsByDate(byDate);
      setLoading(false);
      return byDate;
    } catch (err) {
      console.warn('[useCalendarEvents] Fetch failed:', err.message);
      setError("Couldn't load calendar events. Check your connection and try again.");
      setLoading(false);
      // Keep stale cache if available
      if (cached) {
        setEventsByDate(cached.byDate);
        if (cached.calendarStatus) {
          setCalendarStatus(cached.calendarStatus);
        }
      }
      return null;
    }
  }, [fetchCalendarStatus]);

  // Fetch when viewDate month changes
  useEffect(() => {
    fetchMonth(viewDate);
  }, [monthKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const navigateMonth = useCallback((direction) => {
    setViewDate((prev) =>
      direction > 0 ? addMonths(prev, 1) : subMonths(prev, 1)
    );
  }, []);

  const goToToday = useCallback(() => {
    setViewDate(new Date());
  }, []);

  const refetch = useCallback(() => {
    // Invalidate cache for current month
    delete cacheRef.current[monthKey];
    return fetchMonth(viewDate);
  }, [monthKey, viewDate, fetchMonth]);

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
    eventsByDate,
    loading,
    error,
    calendarStatus,
    navigateMonth,
    goToToday,
    refetch,
  };
}
