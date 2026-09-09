import { useState, useMemo, useCallback, useRef, useEffect } from 'react';
import PropTypes from 'prop-types';
import {
  format,
  startOfMonth,
  endOfMonth,
  startOfWeek,
  endOfWeek,
  addDays,
  isSameMonth,
  isSameDay,
  isToday,
  isBefore,
  startOfDay,
} from 'date-fns';
import { useCalendarEvents } from '../hooks/useCalendarEvents';
import { apiFetch } from '../hooks/useViolaApi';
import { formatTimeDisplay } from '../utils/timeFormat';
import { isFeatureAvailable } from '../utils/featureSurface';
import DesktopUpsell from './DesktopUpsell';
import styles from './CalendarView.module.css';

// The user's calendar links to their Google/Microsoft account through the
// desktop hub. It is hidden only in the credential-less cloud web build with a
// desktop upsell; paired spokes render the same live calendar as the hub.

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export function computeLocalCalendarEndTime(startIso) {
  if (!startIso || !startIso.includes('T')) return null;
  const d = new Date(startIso);
  if (Number.isNaN(d.getTime())) return null;
  d.setHours(d.getHours() + 1);
  return format(d, "yyyy-MM-dd'T'HH:mm:ss");
}

/**
 * Interactive Calendar - Month grid with day expansion.
 *
 * Props:
 *   theme              - THEME object (theme.colors.*)
 *   timeFormat         - '12h' | '24h' | 'auto'
 *   compact            - if true, render current-week strip + today's events (default sidebar view)
 *   onModalOpenChange  - optional callback(isOpen); fires whenever the compact
 *                        mode's expanded-calendar MODAL (not the inline expanded
 *                        view) opens/closes, so a parent that tracks "any modal
 *                        open" (SmartDisplay's anyModalOpen -> aria-hidden/inert
 *                        on background content) can include this one. Without
 *                        it, background text behind the modal stayed visible
 *                        and interactive -- the #2568 fix.
 */
function CalendarViewInner({ theme, timeFormat = 'auto', compact: compactProp = true, onModalOpenChange }) {
  const colors = theme?.colors || {};
  const {
    viewDate,
    eventsByDate,
    loading,
    error,
    calendarStatus,
    navigateMonth,
    goToToday,
    refetch,
  } = useCalendarEvents();

  const [selectedDay, setSelectedDay] = useState(null);
  const [expanded, setExpanded] = useState(!compactProp);
  const [modalOpen, setModalOpen] = useState(false);
  const [hoveredNav, setHoveredNav] = useState(null);
  const hasShownExpandHintRef = useRef(false);
  const [shouldAnimateExpandHint, setShouldAnimateExpandHint] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newEventTitle, setNewEventTitle] = useState('');
  const [newEventTime, setNewEventTime] = useState('');
  const [newEventDescription, setNewEventDescription] = useState('');
  const [formError, setFormError] = useState('');
  const [formSaving, setFormSaving] = useState(false);
  const [editingEventId, setEditingEventId] = useState(null);
  const [editTitle, setEditTitle] = useState('');
  const [editTime, setEditTime] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);
  const [editError, setEditError] = useState('');
  const [editSaving, setEditSaving] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [deletingEventId, setDeletingEventId] = useState(null);

  useEffect(() => {
    if (!expanded && !hasShownExpandHintRef.current) {
      setShouldAnimateExpandHint(true);
      hasShownExpandHintRef.current = true;

      const timer = setTimeout(() => {
        setShouldAnimateExpandHint(false);
      }, 2000);

      return () => clearTimeout(timer);
    }

    return undefined;
  }, [expanded]);

  const getDefaultEventTime = useCallback((day) => {
    if (!day) return '';
    return `${format(day, 'yyyy-MM-dd')}T09:00`;
  }, []);

  const buildEventStartTime = useCallback((value, day) => {
    if (value) {
      return value.length === 16 ? `${value}:00` : value;
    }
    if (!day) return '';
    return `${format(day, 'yyyy-MM-dd')}`;  // all-day: date only, no time
  }, []);

  const computeEndTime = useCallback((startIso) => {
    return computeLocalCalendarEndTime(startIso);
  }, []);

  const getEventTimeInputValue = useCallback((event) => {
    if (!event?.start_time || event.time === 'All day') {
      return '';
    }

    const parsed = new Date(event.start_time);
    if (Number.isNaN(parsed.getTime())) {
      return '';
    }

    return format(parsed, "yyyy-MM-dd'T'HH:mm");
  }, []);

  const resetAddForm = useCallback(() => {
    setNewEventTitle('');
    setNewEventTime('');
    setNewEventDescription('');
    setFormError('');
    setFormSaving(false);
  }, []);

  const resetEditForm = useCallback(() => {
    setEditingEventId(null);
    setEditTitle('');
    setEditTime('');
    setEditDescription('');
    setEditError('');
    setEditSaving(false);
  }, []);

  useEffect(() => {
    resetAddForm();
    resetEditForm();
    setShowAddForm(false);
    setDeleteConfirmId(null);
    setDeleteError('');
    setDeletingEventId(null);
  }, [selectedDay, resetAddForm, resetEditForm]);

  // Build the calendar grid
  const { weeks, allDays } = useMemo(() => {
    const monthStart = startOfMonth(viewDate);
    const monthEnd = endOfMonth(viewDate);
    const gridStart = startOfWeek(monthStart, { weekStartsOn: 0 });
    const gridEnd = endOfWeek(monthEnd, { weekStartsOn: 0 });

    const days = [];
    let day = gridStart;
    while (day <= gridEnd) {
      days.push(day);
      day = addDays(day, 1);
    }

    const wks = [];
    for (let i = 0; i < days.length; i += 7) {
      wks.push(days.slice(i, i + 7));
    }

    return { weeks: wks, allDays: days };
  }, [viewDate]);

  // Current week for compact mode
  const currentWeek = useMemo(() => {
    const today = new Date();
    for (const week of weeks) {
      if (week.some((d) => isSameDay(d, today))) return week;
    }
    return weeks[0] || [];
  }, [weeks]);

  const compactDay = selectedDay || new Date();
  const compactDateKey = format(compactDay, 'yyyy-MM-dd');
  const compactDayEvents = eventsByDate[compactDateKey] || [];
  const compactDayLabel = isToday(compactDay) ? 'Today' : format(compactDay, 'EEE, MMM d');
  const selectedDateKey = selectedDay ? format(selectedDay, 'yyyy-MM-dd') : null;
  const selectedDayEvents = selectedDateKey ? (eventsByDate[selectedDateKey] || []) : [];

  const handleDayClick = useCallback((day) => {
    if (!expanded) {
      setSelectedDay(day);
      return;
    }
    if (selectedDay && isSameDay(selectedDay, day)) {
      setSelectedDay(null);
    } else {
      setSelectedDay(day);
    }
  }, [expanded, selectedDay]);

  const handleExpandToggle = useCallback(() => {
    if (compactProp) {
      setModalOpen(true);
      return;
    }
    setExpanded((prev) => {
      if (prev) setSelectedDay(null);
      return !prev;
    });
  }, [compactProp]);

  const closeExpandedModal = useCallback(() => {
    setModalOpen(false);
  }, []);

  // Collapse day panel when clicking outside
  const handleGridClick = useCallback((e) => {
    if (e.target === e.currentTarget) {
      setSelectedDay(null);
    }
  }, []);

  useEffect(() => {
    if (!modalOpen) return undefined;

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        closeExpandedModal();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [closeExpandedModal, modalOpen]);

  // Tell the parent (TopBar -> SmartDisplay's anyModalOpen) this modal's open
  // state so background page content gets aria-hidden/inert while it's open,
  // same as every other modal in the app (Modal.jsx's shared pattern).
  useEffect(() => {
    onModalOpenChange?.(modalOpen);
  }, [modalOpen, onModalOpenChange]);

  // Report closed on unmount so a parent's "any modal open" union doesn't get
  // stuck true if this component unmounts while its modal happens to be open.
  useEffect(() => {
    return () => onModalOpenChange?.(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleAddFormOpen = useCallback(() => {
    setDeleteConfirmId(null);
    resetEditForm();
    setFormError('');
    setNewEventTime((current) => current || getDefaultEventTime(selectedDay));
    setShowAddForm(true);
  }, [getDefaultEventTime, resetEditForm, selectedDay]);

  const handleAddFormCancel = useCallback(() => {
    resetAddForm();
    setShowAddForm(false);
  }, [resetAddForm]);

  const handleCreateEvent = useCallback(async () => {
    const trimmedTitle = newEventTitle.trim();
    const trimmedDescription = newEventDescription.trim();
    const startTime = buildEventStartTime(newEventTime, selectedDay);

    if (!trimmedTitle) {
      setFormError('Event title is required');
      return;
    }

    if (!startTime) {
      setFormError('Event time is required');
      return;
    }

    setFormSaving(true);
    setFormError('');

    try {
      const endTime = computeEndTime(startTime);
      const body = {
        title: trimmedTitle,
        start_time: startTime,
        ...(endTime ? { end_time: endTime } : {}),
        ...(!startTime.includes('T') ? { all_day: true } : {}),
        ...(trimmedDescription ? { description: trimmedDescription } : {}),
      };

      const result = await apiFetch('/v1/calendar/events', {
        method: 'POST',
        body: JSON.stringify(body),
        headers: { 'Content-Type': 'application/json' },
      });
      if (result && result.ok === false) {
        setFormError(result.error?.message || result.message || 'Failed to save event');
        return;
      }
      resetAddForm();
      setShowAddForm(false);
      await refetch();
    } catch (err) {
      setFormError(err?.message || 'Failed to save event');
    } finally {
      setFormSaving(false);
    }
  }, [
    buildEventStartTime,
    computeEndTime,
    newEventDescription,
    newEventTime,
    newEventTitle,
    refetch,
    resetAddForm,
    selectedDay,
  ]);

  const handleEditEvent = useCallback((event) => {
    setShowAddForm(false);
    resetAddForm();
    setDeleteConfirmId(null);
    setDeleteError('');
    setEditingEventId(event.event_id);
    setEditTitle(event.title || '');
    setEditTime(getEventTimeInputValue(event));
    setEditDescription(event.description || '');
    setEditError('');
  }, [getEventTimeInputValue, resetAddForm]);

  const handleEditCancel = useCallback(() => {
    resetEditForm();
  }, [resetEditForm]);

  const handleUpdateEvent = useCallback(async () => {
    const trimmedTitle = editTitle.trim();
    const trimmedDescription = editDescription.trim();
    const startTime = buildEventStartTime(editTime, selectedDay);

    if (!editingEventId) {
      return;
    }

    if (!trimmedTitle) {
      setEditError('Event title is required');
      return;
    }

    if (!startTime) {
      setEditError('Event time is required');
      return;
    }

    setEditSaving(true);
    setEditError('');

    try {
      const endTime = computeEndTime(startTime);
      const body = {
        title: trimmedTitle,
        start_time: startTime,
        ...(endTime ? { end_time: endTime } : {}),
        ...(!startTime.includes('T') ? { all_day: true } : {}),
        ...(trimmedDescription ? { description: trimmedDescription } : {}),
      };

      const result = await apiFetch(`/v1/calendar/events/${editingEventId}`, {
        method: 'PUT',
        body: JSON.stringify(body),
        headers: { 'Content-Type': 'application/json' },
      });
      if (result && result.ok === false) {
        setEditError(result.error?.message || result.message || 'Failed to update event');
        return;
      }
      resetEditForm();
      await refetch();
    } catch (err) {
      setEditError(err?.message || 'Failed to update event');
    } finally {
      setEditSaving(false);
    }
  }, [
    buildEventStartTime,
    computeEndTime,
    editDescription,
    editTime,
    editTitle,
    editingEventId,
    refetch,
    resetEditForm,
    selectedDay,
  ]);

  const handleDeletePrompt = useCallback((eventId) => {
    setShowAddForm(false);
    resetAddForm();
    resetEditForm();
    setDeleteConfirmId(eventId);
    setDeleteError('');
  }, [resetAddForm, resetEditForm]);

  const handleDeleteCancel = useCallback(() => {
    setDeleteConfirmId(null);
    setDeleteError('');
    setDeletingEventId(null);
  }, []);

  const handleDeleteEvent = useCallback(async (eventId) => {
    setDeletingEventId(eventId);
    setDeleteError('');

    try {
      await apiFetch(`/v1/calendar/events/${eventId}`, {
        method: 'DELETE',
      });
      setDeleteConfirmId(null);
      await refetch();
    } catch (err) {
      setDeleteError(err?.message || 'Failed to delete event');
    } finally {
      setDeletingEventId(null);
    }
  }, [refetch]);

  // --- Shared sub-components ---

  const renderNavArrow = (direction, label) => {
    const id = direction === -1 ? 'prev' : 'next';
    return (
      <div
        onClick={(e) => { e.stopPropagation(); navigateMonth(direction); }}
        onMouseOver={() => setHoveredNav(id)}
        onMouseOut={() => setHoveredNav(null)}
        style={{
          cursor: 'pointer',
          padding: '4px 8px',
          borderRadius: '6px',
          color: colors.accent,
          opacity: hoveredNav === id ? 1 : 0.7,
          background: hoveredNav === id ? colors.glassHover : 'transparent',
          transition: 'background 0.2s ease, opacity 0.2s ease',
          display: 'flex',
          alignItems: 'center',
          userSelect: 'none',
        }}
        title={label}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {direction === -1
            ? <polyline points="15 18 9 12 15 6" />
            : <polyline points="9 6 15 12 9 18" />
          }
        </svg>
      </div>
    );
  };

  const renderDayCell = (day, isCompactRow = false) => {
    const dateKey = format(day, 'yyyy-MM-dd');
    const dayEvents = eventsByDate[dateKey] || [];
    const hasEvents = dayEvents.length > 0;
    const inMonth = isSameMonth(day, viewDate);
    const today = isToday(day);
    const isPast = isBefore(startOfDay(day), startOfDay(new Date())) && !today;
    const isSelected = selectedDay && isSameDay(selectedDay, day);
    return (
      <div
        key={dateKey}
        data-testid={`calendar-day-${dateKey}`}
        className={styles.dayCell}
        onClick={() => handleDayClick(day)}
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          borderRadius: '8px',
          padding: isCompactRow ? '4px 2px' : 'clamp(4px, 0.5vh, 8px) 2px',
          minHeight: isCompactRow ? '36px' : 'clamp(32px, 4vh, 44px)',
          '--calendar-day-bg': isSelected ? colors.glassActive : 'transparent',
          '--calendar-day-hover-bg': isSelected ? colors.glassActive : colors.glassHover,
          border: today
            ? `1.5px solid ${colors.accent}`
            : '1.5px solid transparent',
          boxShadow: today ? `0 0 8px ${colors.accentGlow}` : 'none',
          opacity: inMonth ? (isPast ? 0.45 : 1) : 0.2,
          transition: 'background 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease, opacity 0.2s ease',
          position: 'relative',
          userSelect: 'none',
        }}
      >
        <span style={{
          fontSize: isCompactRow ? 'clamp(12px, 1.5vw, 15px)' : 'clamp(11px, 1.3vw, 14px)',
          fontWeight: today ? 500 : 400,
          color: today ? colors.accent : colors.textPrimary,
          lineHeight: 1.2,
        }}>
          {format(day, 'd')}
        </span>
        {hasEvents && (
          <div style={{
            width: '4px',
            height: '4px',
            borderRadius: '50%',
            backgroundColor: colors.accent,
            marginTop: '2px',
            flexShrink: 0,
          }} />
        )}
      </div>
    );
  };

  const renderEventItem = (event, idx, interactive = false) => {
    const isGoogle = event.source === 'google';
    const isAllDay = event.time === 'All day';
    const isPast = event.start_time && new Date(event.start_time) < new Date() && !isAllDay;
    const eventId = event.event_id || `event-${idx}`;
    const inputStyle = {
      width: '100%',
      background: colors.bgElevated,
      border: `1px solid ${colors.borderLight}`,
      color: colors.textPrimary,
      borderRadius: '6px',
      padding: '8px',
      fontSize: 'clamp(12px, 1.4vw, 14px)',
      outline: 'none',
      boxSizing: 'border-box',
      transition: 'border-color 0.2s ease',
    };

    if (interactive && deleteConfirmId === eventId) {
      return (
        <div key={eventId} style={{
          padding: '10px',
          borderLeft: `2px solid ${colors.statusRed}`,
          background: colors.glassBase,
          borderRadius: '8px',
        }}>
          <div style={{
            fontSize: 'clamp(12px, 1.4vw, 14px)',
            color: colors.textPrimary,
            marginBottom: '8px',
          }}>
            Delete this event?
          </div>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
          }}>
            <button
              type="button"
              onClick={() => handleDeleteEvent(eventId)}
              disabled={deletingEventId === eventId}
              style={{
                border: 'none',
                background: 'transparent',
                color: colors.statusRed,
                cursor: deletingEventId === eventId ? 'default' : 'pointer',
                padding: 0,
                fontSize: 'clamp(12px, 1.4vw, 14px)',
                opacity: deletingEventId === eventId ? 0.6 : 1,
              }}
            >
              {deletingEventId === eventId ? 'Deleting...' : 'Yes'}
            </button>
            <button
              type="button"
              onClick={handleDeleteCancel}
              style={{
                border: 'none',
                background: 'transparent',
                color: colors.textMuted,
                cursor: 'pointer',
                padding: 0,
                fontSize: 'clamp(12px, 1.4vw, 14px)',
              }}
            >
              No
            </button>
          </div>
          {deleteError && (
            <div style={{
              marginTop: '8px',
              fontSize: 'clamp(11px, 1.3vw, 13px)',
              color: colors.statusRed,
            }}>
              {deleteError}
            </div>
          )}
        </div>
      );
    }

    if (interactive && editingEventId === eventId) {
      return (
        <div key={eventId} style={{
          display: 'flex',
          flexDirection: 'column',
          gap: '8px',
          padding: '10px',
          borderLeft: `2px solid ${colors.accent}`,
          background: colors.glassBase,
          borderRadius: '8px',
        }}>
          <input
            value={editTitle}
            onChange={(e) => setEditTitle(e.target.value)}
            placeholder="Event title"
            style={inputStyle}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = colors.accent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = colors.borderLight;
            }}
          />
          <input
            type="datetime-local"
            value={editTime}
            onChange={(e) => setEditTime(e.target.value)}
            style={inputStyle}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = colors.accent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = colors.borderLight;
            }}
          />
          <input
            value={editDescription}
            onChange={(e) => setEditDescription(e.target.value)}
            placeholder="Description (optional)"
            style={inputStyle}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = colors.accent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = colors.borderLight;
            }}
          />
          <div style={{
            display: 'flex',
            gap: '8px',
          }}>
            <button
              type="button"
              onClick={handleUpdateEvent}
              disabled={editSaving}
              style={{
                flex: 1,
                border: 'none',
                borderRadius: '6px',
                padding: '8px 10px',
                background: colors.accent,
                color: colors.textPrimary,
                cursor: editSaving ? 'default' : 'pointer',
                opacity: editSaving ? 0.7 : 1,
              }}
            >
              {editSaving ? 'Saving...' : 'Save'}
            </button>
            <button
              type="button"
              onClick={handleEditCancel}
              style={{
                flex: 1,
                border: `1px solid ${colors.borderLight}`,
                borderRadius: '6px',
                padding: '8px 10px',
                background: 'transparent',
                color: colors.textMuted,
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
          {editError && (
            <div style={{
              fontSize: 'clamp(11px, 1.3vw, 13px)',
              color: colors.statusRed,
            }}>
              {editError}
            </div>
          )}
        </div>
      );
    }

    return (
      <div
        key={eventId}
        className={interactive ? styles.eventItemInteractive : undefined}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
          padding: '6px 8px',
          opacity: isPast ? 0.5 : 1,
          borderLeft: `2px solid ${colors.accent}`,
          '--calendar-event-hover-bg': colors.glassBase,
          transition: 'opacity 0.3s ease, background 0.2s ease',
          borderRadius: '8px',
        }}
      >
        <span style={{
          fontSize: 'clamp(11px, 1.3vw, 13px)',
          color: colors.textMuted,
          fontWeight: 400,
          minWidth: 'clamp(46px, 6vw, 64px)',
          flexShrink: 0,
        }}>
          {isAllDay
            ? 'All day'
            : (event.start_time
              ? formatTimeDisplay(event.start_time, timeFormat)
              : (event.time || '')
            )
          }
        </span>
        <span style={{
          fontSize: 'clamp(12px, 1.5vw, 15px)',
          color: colors.textPrimary,
          fontWeight: 400,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          flex: 1,
        }}>
          {event.title}
        </span>
        {isGoogle && (
          <span style={{
            fontSize: '10px',
            color: colors.textMuted,
            opacity: 0.6,
            flexShrink: 0,
            fontWeight: 500,
            letterSpacing: '0.3px',
          }}>
            G
          </span>
        )}
        {interactive && (
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            flexShrink: 0,
          }}>
            <button
              type="button"
              onClick={() => handleEditEvent(event)}
              className={styles.eventActionButton}
              style={{
                border: 'none',
                background: 'transparent',
                color: colors.textMuted,
                cursor: 'pointer',
                padding: 0,
                fontSize: 'clamp(12px, 1.4vw, 14px)',
                lineHeight: 1,
              }}
              aria-label="Edit event"
            >
              ✎
            </button>
            <button
              type="button"
              onClick={() => handleDeletePrompt(eventId)}
              className={styles.eventActionButton}
              style={{
                border: 'none',
                background: 'transparent',
                color: colors.textMuted,
                cursor: 'pointer',
                padding: 0,
                fontSize: 'clamp(14px, 1.6vw, 16px)',
                lineHeight: 1,
              }}
              aria-label="Delete event"
            >
              ×
            </button>
          </div>
        )}
      </div>
    );
  };

  // Loading shimmer bar
  const renderShimmer = () => (
    <div style={{
      height: '3px',
      borderRadius: '2px',
      background: `linear-gradient(90deg, transparent, ${colors.glassActive}, transparent)`,
      animation: 'shimmer 1.5s infinite',
      margin: '8px 0',
    }}>
      <style>{`
        @keyframes shimmer {
          0% { background-position: -200px 0; }
          100% { background-position: 200px 0; }
        }
      `}</style>
    </div>
  );

  // --- Error state ---
  if (error && Object.keys(eventsByDate).length === 0) {
    return (
      <div
        onClick={refetch}
        style={{
          padding: '12px 0 4px 0',
          cursor: 'pointer',
          color: colors.textMuted,
          fontSize: 'clamp(12px, 1.5vw, 15px)',
        }}
        title="Try again"
      >
        Calendar couldn't load. Try again.
      </div>
    );
  }

  // ============================================================
  // COMPACT MODE — week strip + today's events
  // ============================================================
  const renderCompactCalendar = () => (
      <div data-testid="calendar-compact" style={{ padding: '0 0 2px 0' }}>
        {/* Week strip header */}
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: '4px',
          }}
        >
          {/* Day name headers */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(7, 1fr)',
            gap: '2px',
            textAlign: 'center',
          }}>
            {DAY_NAMES.map((name) => (
              <span key={name} style={{
                fontSize: 'clamp(9px, 1vw, 11px)',
                color: colors.textMuted,
                fontWeight: 400,
                textTransform: 'uppercase',
                letterSpacing: '0.5px',
              }}>
                {name}
              </span>
            ))}
          </div>

          {/* Current week days */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(7, 1fr)',
            gap: '2px',
          }}>
            {currentWeek.map((day) => renderDayCell(day, true))}
          </div>

          <div
            data-testid="calendar-expand-button"
            onClick={(e) => { e.stopPropagation(); handleExpandToggle(); }}
            onMouseOver={(e) => {
              e.currentTarget.style.opacity = '0.7';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.opacity = '0.3';
            }}
            style={{
              alignSelf: 'center',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: colors.textMuted,
              opacity: 0.3,
              cursor: 'pointer',
              userSelect: 'none',
              lineHeight: 1,
              transition: 'opacity 0.2s ease',
              animation: shouldAnimateExpandHint ? 'calendarExpandHint 2s ease-in-out 1' : 'none',
            }}
            title="Expand to full month view"
          >
            <style>{`
              @keyframes calendarExpandHint {
                0% { opacity: 0.3; }
                50% { opacity: 0.6; }
                100% { opacity: 0.3; }
              }
            `}</style>
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </div>
        </div>

        {loading && renderShimmer()}

        <div
          data-testid="calendar-compact-events"
          style={{
            marginTop: '8px',
            display: 'flex',
            flexDirection: 'column',
            gap: '2px',
          }}
        >
          <span style={{
            color: colors.textMuted,
            fontSize: 'clamp(10px, 1.2vw, 12px)',
            fontWeight: 700,
            paddingLeft: '10px',
            textTransform: 'uppercase',
          }}>
            {compactDayLabel}
          </span>
          {compactDayEvents.length > 0 && (
            <>
              {compactDayEvents.slice(0, 5).map((event, idx) => renderEventItem(event, idx))}
              {compactDayEvents.length > 5 && (
                <span style={{
                  fontSize: 'clamp(11px, 1.3vw, 13px)',
                  color: colors.textMuted,
                  paddingLeft: '10px',
                  paddingTop: '4px',
                }}>
                  +{compactDayEvents.length - 5} more
                </span>
              )}
            </>
          )}

          {!loading && compactDayEvents.length === 0 && (
            <div style={{
              padding: '2px 0 0 10px',
              fontSize: 'clamp(12px, 1.5vw, 15px)',
              color: colors.textMuted,
            }}>
              {isToday(compactDay) ? 'No events today' : 'No events'}
            </div>
          )}
        </div>

        {modalOpen && (
          <div
            data-testid="calendar-expanded-modal"
            role="dialog"
            aria-modal="true"
            aria-label="Expanded calendar"
            className={styles.modalOverlay}
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) {
                closeExpandedModal();
              }
            }}
            style={{
              '--calendar-modal-overlay-bg': colors.overlay || 'rgba(0, 0, 0, 0.82)',
            }}
          >
            <div
              data-testid="calendar-expanded-surface"
              className={styles.modalSurface}
              onMouseDown={(event) => event.stopPropagation()}
              style={{
                '--calendar-modal-border': colors.borderHover,
                '--calendar-modal-bg-top': colors.bgElevated,
                '--calendar-modal-bg-bottom': colors.bgCard,
                '--calendar-modal-shadow': colors.shadowDeep || 'rgba(0,0,0,0.8)',
                '--calendar-modal-inner-border': colors.borderLight,
              }}
            >
              {renderExpandedCalendar({ modal: true })}
            </div>
          </div>
        )}
      </div>
  );

  // ============================================================
  // EXPANDED MODE — full month grid with day expansion
  // ============================================================
  const renderExpandedCalendar = ({ modal = false } = {}) => {
    const closeExpandedView = modal ? closeExpandedModal : handleExpandToggle;

    return (
      <div
        data-testid={modal ? 'calendar-expanded-content' : 'calendar-expanded-inline'}
        style={{
          padding: modal ? '0' : '8px 0 4px 0',
          transition: 'opacity 0.3s ease, transform 0.3s ease',
        }}
      >
      {/* Month header with navigation */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: '8px',
      }}>
        {renderNavArrow(-1, 'Previous month')}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span
            onClick={closeExpandedView}
            style={{
              fontSize: 'clamp(14px, 1.8vw, 18px)',
              fontWeight: 500,
              color: colors.textPrimary,
              cursor: 'pointer',
              userSelect: 'none',
            }}
            title={modal ? 'Close expanded calendar' : 'Collapse to week view'}
          >
            {format(viewDate, 'MMMM yyyy')}
          </span>
          {!isSameMonth(viewDate, new Date()) && (
            <div
              onClick={(e) => { e.stopPropagation(); goToToday(); }}
              onMouseOver={() => setHoveredNav('today')}
              onMouseOut={() => setHoveredNav(null)}
              style={{
                fontSize: 'clamp(10px, 1.2vw, 12px)',
                color: colors.accent,
                cursor: 'pointer',
                padding: '2px 8px',
                borderRadius: '10px',
                border: `1px solid ${colors.borderLight}`,
                background: hoveredNav === 'today' ? colors.glassHover : 'transparent',
                transition: 'background 0.2s ease, border-color 0.2s ease',
                userSelect: 'none',
              }}
            >
              Today
            </div>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          {renderNavArrow(1, 'Next month')}
          <div
            onClick={(e) => { e.stopPropagation(); closeExpandedView(); }}
            onMouseOver={(e) => {
              e.currentTarget.style.opacity = '0.8';
              e.currentTarget.style.transform = 'scale(1.1)';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.opacity = '0.4';
              e.currentTarget.style.transform = 'scale(1)';
            }}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: colors.textMuted,
              opacity: 0.4,
              cursor: 'pointer',
              userSelect: 'none',
              lineHeight: 1,
              transform: 'scale(1)',
              transition: 'opacity 0.2s ease, transform 0.2s ease',
            }}
            title={modal ? 'Close expanded calendar' : 'Collapse to week view'}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="6 15 12 9 18 15" />
            </svg>
          </div>
        </div>
      </div>

      {loading && renderShimmer()}

      {/* Day name headers */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(7, 1fr)',
        gap: '2px',
        textAlign: 'center',
        marginBottom: '4px',
      }}>
        {DAY_NAMES.map((name) => (
          <span key={name} style={{
            fontSize: 'clamp(9px, 1vw, 11px)',
            color: colors.textMuted,
            fontWeight: 400,
            textTransform: 'uppercase',
            letterSpacing: '0.5px',
          }}>
            {name}
          </span>
        ))}
      </div>

      {/* Month grid */}
      <div onClick={handleGridClick} style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(7, 1fr)',
        gap: '2px',
      }}>
        {allDays.map((day) => renderDayCell(day))}
      </div>

      {/* Expanded day panel */}
      <div style={{
        maxHeight: selectedDay ? '560px' : '0px',
        opacity: selectedDay ? 1 : 0,
        overflow: 'hidden',
        transition: 'max-height 0.3s ease, opacity 0.3s ease',
      }}>
        {selectedDay && (
          <div style={{
            marginTop: '8px',
            padding: '10px',
            borderRadius: '10px',
            background: `linear-gradient(135deg, ${colors.glassBase}, ${colors.glassHover})`,
            border: `1px solid ${colors.borderLight}`,
          }}>
            {/* Day panel header */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: '8px',
            }}>
              <span style={{
                fontSize: 'clamp(12px, 1.5vw, 15px)',
                fontWeight: 500,
                color: colors.textPrimary,
              }}>
                {format(selectedDay, 'EEEE, MMMM d')}
              </span>
              <div
                onClick={() => setSelectedDay(null)}
                style={{
                  cursor: 'pointer',
                  color: colors.textMuted,
                  padding: '2px',
                  display: 'flex',
                  alignItems: 'center',
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </div>
            </div>

            {/* Event list */}
            {selectedDayEvents.length > 0 ? (
              <div style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '4px',
                maxHeight: '220px',
                overflowY: 'auto',
              }}>
                {selectedDayEvents.map((event, idx) => renderEventItem(event, idx, true))}
              </div>
            ) : (
              <span style={{
                fontSize: 'clamp(12px, 1.5vw, 14px)',
                color: colors.textMuted,
                fontStyle: 'italic',
              }}>
                No events
              </span>
            )}
            <div style={{ marginTop: '10px' }}>
              {!showAddForm && (
                <button
                  type="button"
                  onClick={handleAddFormOpen}
                  style={{
                    border: `1px solid ${colors.borderLight}`,
                    borderRadius: '6px',
                    padding: '6px 10px',
                    background: colors.glassBase,
                    color: colors.accent,
                    cursor: 'pointer',
                    transition: 'background 0.2s ease, border-color 0.2s ease',
                  }}
                  onMouseOver={(e) => {
                    e.currentTarget.style.background = colors.glassHover;
                    e.currentTarget.style.borderColor = colors.borderHover;
                  }}
                  onMouseOut={(e) => {
                    e.currentTarget.style.background = colors.glassBase;
                    e.currentTarget.style.borderColor = colors.borderLight;
                  }}
                >
                  + Add event
                </button>
              )}
              <div style={{
                maxHeight: showAddForm ? '260px' : '0px',
                opacity: showAddForm ? 1 : 0,
                overflow: 'hidden',
                transition: 'max-height 0.3s ease, opacity 0.3s ease',
              }}>
                <div style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '8px',
                  marginTop: showAddForm ? '10px' : '0px',
                }}>
                  <input
                    value={newEventTitle}
                    onChange={(e) => setNewEventTitle(e.target.value)}
                    placeholder="Event title"
                    style={{
                      width: '100%',
                      background: colors.bgElevated,
                      border: `1px solid ${colors.borderLight}`,
                      color: colors.textPrimary,
                      borderRadius: '6px',
                      padding: '8px',
                      fontSize: 'clamp(12px, 1.4vw, 14px)',
                      outline: 'none',
                      boxSizing: 'border-box',
                      transition: 'border-color 0.2s ease',
                    }}
                    onFocus={(e) => {
                      e.currentTarget.style.borderColor = colors.accent;
                    }}
                    onBlur={(e) => {
                      e.currentTarget.style.borderColor = colors.borderLight;
                    }}
                  />
                  <input
                    type="datetime-local"
                    value={newEventTime}
                    onChange={(e) => setNewEventTime(e.target.value)}
                    style={{
                      width: '100%',
                      background: colors.bgElevated,
                      border: `1px solid ${colors.borderLight}`,
                      color: colors.textPrimary,
                      borderRadius: '6px',
                      padding: '8px',
                      fontSize: 'clamp(12px, 1.4vw, 14px)',
                      outline: 'none',
                      boxSizing: 'border-box',
                      transition: 'border-color 0.2s ease',
                    }}
                    onFocus={(e) => {
                      e.currentTarget.style.borderColor = colors.accent;
                    }}
                    onBlur={(e) => {
                      e.currentTarget.style.borderColor = colors.borderLight;
                    }}
                  />
                  <input
                    value={newEventDescription}
                    onChange={(e) => setNewEventDescription(e.target.value)}
                    placeholder="Description (optional)"
                    style={{
                      width: '100%',
                      background: colors.bgElevated,
                      border: `1px solid ${colors.borderLight}`,
                      color: colors.textPrimary,
                      borderRadius: '6px',
                      padding: '8px',
                      fontSize: 'clamp(12px, 1.4vw, 14px)',
                      outline: 'none',
                      boxSizing: 'border-box',
                      transition: 'border-color 0.2s ease',
                    }}
                    onFocus={(e) => {
                      e.currentTarget.style.borderColor = colors.accent;
                    }}
                    onBlur={(e) => {
                      e.currentTarget.style.borderColor = colors.borderLight;
                    }}
                  />
                  <div style={{
                    display: 'flex',
                    gap: '8px',
                  }}>
                    <button
                      type="button"
                      onClick={handleCreateEvent}
                      disabled={formSaving}
                      style={{
                        flex: 1,
                        border: 'none',
                        borderRadius: '6px',
                        padding: '8px 10px',
                        background: colors.accent,
                        color: colors.textPrimary,
                        cursor: formSaving ? 'default' : 'pointer',
                        opacity: formSaving ? 0.7 : 1,
                      }}
                    >
                      {formSaving ? 'Saving...' : 'Save'}
                    </button>
                    <button
                      type="button"
                      onClick={handleAddFormCancel}
                      style={{
                        flex: 1,
                        border: `1px solid ${colors.borderLight}`,
                        borderRadius: '6px',
                        padding: '8px 10px',
                        background: 'transparent',
                        color: colors.textMuted,
                        cursor: 'pointer',
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                  {formError && (
                    <div style={{
                      fontSize: 'clamp(11px, 1.3vw, 13px)',
                      color: colors.statusRed,
                    }}>
                      {formError}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
      </div>
    );
  };

  if (!expanded) {
    return renderCompactCalendar();
  }

  return renderExpandedCalendar();
}

const calendarViewPropTypes = {
  theme: PropTypes.shape({
    colors: PropTypes.object,
  }),
  timeFormat: PropTypes.string,
  compact: PropTypes.bool,
  onModalOpenChange: PropTypes.func,
};

CalendarViewInner.propTypes = calendarViewPropTypes;

/**
 * CalendarView - local-primary calendar CRUD is CLOUD_NATIVE (see
 * backend/cloud_route_manifest.py "calendar" group, tenant-safety reviewed
 * 2026-07-06) and renders live everywhere, including the cloud SPA. `calendar`
 * is intentionally NOT in featureSurface.DESKTOP_ONLY_FEATURES; this gate
 * stays in place only so a future genuinely-desktop-only calendar mode (e.g.
 * Google/Microsoft OAuth sync) can reuse the same upsell without a second
 * branch here.
 */
export default function CalendarView(props) {
  if (!isFeatureAvailable('calendar')) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: '24px' }}>
        <DesktopUpsell feature="calendar" />
      </div>
    );
  }
  return <CalendarViewInner {...props} />;
}

CalendarView.propTypes = calendarViewPropTypes;
