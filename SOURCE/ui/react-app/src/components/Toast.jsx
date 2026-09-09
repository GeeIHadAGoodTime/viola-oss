import { useState, useEffect, useCallback, useRef } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';

// =========================================================================
// Toast Notification System
//
// Provides stackable, auto-dismissing toast notifications with support for
// error, warning, and info levels. Designed to surface backend ErrorContext
// user_message fields and other UI-facing errors.
//
// Usage:
//   const { toasts, addToast, removeToast } = useToast();
//   addToast({ message: 'Something happened', level: 'error' });
//   <ToastContainer toasts={toasts} onDismiss={removeToast} />
// =========================================================================

const ANIMATION_MS = 300;
const MAX_TOASTS = 5;

// Severity-based auto-dismiss durations
function getAutoDismissMs(level) {
  if (level === 'error') return 10000;
  if (level === 'warning') return 7000;
  return 5000; // info / default
}

// Icons per severity level (static JSX, no color dependency)
const LEVEL_ICONS = {
  error: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="15" y1="9" x2="9" y2="15" />
      <line x1="9" y1="9" x2="15" y2="15" />
    </svg>
  ),
  warning: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  info: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  ),
};

// Color mapping per severity level — read at render time so theme changes apply
function getLevelStyles(level) {
  const map = {
    error:   { borderColor: THEME.colors.statusRed,    iconColor: THEME.colors.statusRed },
    warning: { borderColor: THEME.colors.statusYellow,  iconColor: THEME.colors.statusYellow },
    info:    { borderColor: THEME.colors.accent,        iconColor: THEME.colors.accent },
  };
  const colors = map[level] || map.info;
  return { ...colors, icon: LEVEL_ICONS[level] || LEVEL_ICONS.info };
}

// -------------------------------------------------------------------------
// Individual Toast item
// -------------------------------------------------------------------------
const ToastItem = ({ id, message, level, persist, onDismiss }) => {
  const [visible, setVisible] = useState(false);
  const [exiting, setExiting] = useState(false);
  const timerRef = useRef(null);

  const styles = getLevelStyles(level);

  const handleDismiss = useCallback(() => {
    setExiting(true);
    // Wait for exit animation, then remove from DOM
    setTimeout(() => {
      onDismiss(id);
    }, ANIMATION_MS);
  }, [id, onDismiss]);

  // Slide in on mount
  useEffect(() => {
    // Small delay so the browser registers the initial transform for animation
    const frameId = requestAnimationFrame(() => {
      setVisible(true);
    });
    return () => cancelAnimationFrame(frameId);
  }, []);

  // Auto-dismiss timer (skipped when persist=true)
  useEffect(() => {
    if (persist) return;
    timerRef.current = setTimeout(handleDismiss, getAutoDismissMs(level));
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [handleDismiss, level, persist]);

  return (
    <div
      role="status"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        padding: '14px 18px',
        backgroundColor: `${THEME.colors.bgElevated}F0`,
        borderRadius: '12px',
        borderLeft: `3px solid ${styles.borderColor}`,
        boxShadow: `0 8px 32px ${THEME.colors.shadowHeavy}, inset 0 0 0 1px ${THEME.colors.borderLight}`,
        color: THEME.colors.textSecondary,
        fontSize: '14px',
        lineHeight: 1.4,
        maxWidth: '420px',
        width: '100%',
        boxSizing: 'border-box',
        // Slide-in / slide-out animation
        transform: exiting
          ? 'translateX(120%)'
          : visible
            ? 'translateX(0)'
            : 'translateX(120%)',
        opacity: exiting ? 0 : visible ? 1 : 0,
        transition: `transform ${ANIMATION_MS}ms ease, opacity ${ANIMATION_MS}ms ease`,
        pointerEvents: 'auto',
      }}
    >
      {/* Icon */}
      <div style={{ color: styles.iconColor, flexShrink: 0, display: 'flex', alignItems: 'center' }}>
        {styles.icon}
      </div>

      {/* Message text */}
      <div style={{ flex: 1, wordBreak: 'break-word' }}>
        {message}
      </div>

      {/* Dismiss button */}
      <button
        onClick={handleDismiss}
        aria-label="Dismiss notification"
        style={{
          background: 'none',
          border: 'none',
          color: THEME.colors.textDisabled,
          cursor: 'pointer',
          padding: '12px',
          margin: '-8px',
          display: 'flex',
          alignItems: 'center',
          flexShrink: 0,
          transition: 'color 0.15s ease',
        }}
        onMouseOver={e => { e.currentTarget.style.color = THEME.colors.textSecondary; }}
        onMouseOut={e => { e.currentTarget.style.color = THEME.colors.textDisabled; }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
  );
};

ToastItem.propTypes = {
  id: PropTypes.number.isRequired,
  message: PropTypes.string.isRequired,
  level: PropTypes.oneOf(['error', 'warning', 'info']).isRequired,
  persist: PropTypes.bool,
  onDismiss: PropTypes.func.isRequired,
};

// -------------------------------------------------------------------------
// Toast container - positioned fixed, holds the stack of toasts
// -------------------------------------------------------------------------
const ToastContainer = ({ toasts, onDismiss }) => {
  if (!toasts || toasts.length === 0) return null;

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      aria-relevant="additions removals"
      style={{
        position: 'fixed',
        bottom: '24px',
        right: '24px',
        display: 'flex',
        flexDirection: 'column-reverse',
        gap: '10px',
        zIndex: 9999,
        pointerEvents: 'none',
      }}
    >
      {toasts.map(toast => (
        <ToastItem
          key={toast.id}
          id={toast.id}
          message={toast.message}
          level={toast.level}
          persist={toast.persist}
          onDismiss={onDismiss}
        />
      ))}
    </div>
  );
};

ToastContainer.propTypes = {
  toasts: PropTypes.arrayOf(
    PropTypes.shape({
      id: PropTypes.number.isRequired,
      message: PropTypes.string.isRequired,
      level: PropTypes.oneOf(['error', 'warning', 'info']).isRequired,
      persist: PropTypes.bool,
    })
  ).isRequired,
  onDismiss: PropTypes.func.isRequired,
};

// -------------------------------------------------------------------------
// useToast hook - manages toast state
// -------------------------------------------------------------------------
let _nextToastId = 1;

export function useToast() {
  const [toasts, setToasts] = useState([]);

  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  const addToast = useCallback(({ message, level = 'info', persist = false }) => {
    if (!message) return;

    const id = _nextToastId++;
    setToasts(prev => {
      // Limit stack size - drop oldest if at max
      const updated = prev.length >= MAX_TOASTS ? prev.slice(1) : prev;
      return [...updated, { id, message, level, persist }];
    });

    return id;
  }, []);

  return { toasts, addToast, removeToast };
}

export default ToastContainer;
