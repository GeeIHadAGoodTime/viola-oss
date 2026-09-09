import React, { useEffect, useId, useRef } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';

/**
 * Base Modal component
 */
export default function Modal({ isOpen, onClose, title, children, footer }) {
  const dialogRef = useRef(null);
  const titleId = useId();
  const contentId = useId();

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (!isOpen) {
        return;
      }

      if (e.key === 'Escape') {
        onClose();
        return;
      }

      if (e.key === 'Tab' && dialogRef.current) {
        const focusable = dialogRef.current.querySelectorAll(
          'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (focusable.length === 0) {
          e.preventDefault();
          dialogRef.current.focus();
          return;
        }

        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  // Move focus into the dialog and return it on close.
  useEffect(() => {
    if (!isOpen) {
      return undefined;
    }

    const previouslyFocused = document.activeElement;
    const frame = window.requestAnimationFrame(() => {
      const firstFocusable = dialogRef.current?.querySelector(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      (firstFocusable || dialogRef.current)?.focus();
    });

    return () => {
      window.cancelAnimationFrame(frame);
      if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
        previouslyFocused.focus();
      }
    };
  }, [isOpen]);

  // Prevent body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isOpen]);

  if (!isOpen) return null;

  const isPhoneViewport = typeof window !== 'undefined' && window.innerWidth <= 480;
  const dialogWidth = isPhoneViewport ? 'calc(100vw - 16px)' : '90%';
  const dialogMaxWidth = isPhoneViewport ? 'none' : '600px';
  const dialogMaxHeight = isPhoneViewport ? 'calc(100dvh - 16px)' : '85vh';
  const dialogRadius = isPhoneViewport ? '16px' : '24px';
  const shellPadding = isPhoneViewport ? '16px' : '24px';

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: THEME.colors.overlay,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={contentId}
        tabIndex={-1}
        style={{
          backgroundColor: THEME.colors.bgCard,
          borderRadius: dialogRadius,
          width: dialogWidth,
          maxWidth: dialogMaxWidth,
          maxHeight: dialogMaxHeight,
          display: 'flex',
          flexDirection: 'column',
          boxShadow: `0 24px 80px ${THEME.colors.shadowDeep}, inset 0 0 0 1px ${THEME.colors.borderLight}`,
          overflow: 'hidden',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: isPhoneViewport ? '16px' : '20px 24px',
            borderBottom: `1px solid ${THEME.colors.borderLight}`,
          }}
        >
          <h2 id={titleId} style={{ margin: 0, fontSize: '20px', fontWeight: 500, color: THEME.colors.textPrimary }}>
            {title}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close modal"
            style={{
              // 44x44 is the mobile touch-target floor (was a 4px/8px padding
              // box around a 24px glyph, ~30x32 -- see issue #367). The glyph
              // itself stays visually the same size; only the hit area grows.
              width: '44px',
              height: '44px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxSizing: 'border-box',
              background: 'none',
              border: 'none',
              color: THEME.colors.textMuted,
              fontSize: '24px',
              cursor: 'pointer',
              lineHeight: 1,
              borderRadius: '8px',
              transition: 'all 0.15s ease',
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.backgroundColor = THEME.colors.glassBase;
              e.currentTarget.style.color = THEME.colors.textPrimary;
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.backgroundColor = 'transparent';
              e.currentTarget.style.color = THEME.colors.textMuted;
            }}
          >
            &times;
          </button>
        </div>

        {/* Content — the footer lives INSIDE this scroll region (not as a
            sticky sibling) so it flows to the natural end of the content. A
            sticky footer pinned over an internally-scrolling body geometrically
            overlaps whatever content sits at the scroll fold (raw-rect
            overlap), which the display-integrity gate flags; scrolling the
            footer with the content removes that overlap while keeping every
            button identical. */}
        <div
          className="modal-content-body"
          id={contentId}
          style={{
            padding: shellPadding,
            overflowY: 'auto',
            overflowX: 'auto',
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <style>{`
            @media (max-width: 400px) {
              .modal-content-body { padding: 12px !important; }
            }
          `}</style>
          <div style={{ flex: '1 0 auto' }}>
            {children}
          </div>

          {/* Footer (scrolls with content) */}
          {footer && (
            <div
              style={{
                marginTop: isPhoneViewport ? '14px' : '16px',
                paddingTop: isPhoneViewport ? '14px' : '16px',
                borderTop: `1px solid ${THEME.colors.borderLight}`,
                display: 'flex',
                justifyContent: 'flex-end',
                gap: '12px',
                flexWrap: 'wrap',
                flexShrink: 0,
              }}
            >
              {footer}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Reusable button styles
export const modalButtonStyle = {
  padding: '10px 20px',
  minHeight: '44px',
  borderRadius: '12px',
  border: 'none',
  cursor: 'pointer',
  fontSize: '14px',
  fontWeight: 500,
  transition: 'all 0.15s ease',
};

export const primaryButtonStyle = {
  ...modalButtonStyle,
  backgroundColor: THEME.colors.textPrimary,
  color: THEME.colors.bgCard,
};

export const secondaryButtonStyle = {
  ...modalButtonStyle,
  backgroundColor: THEME.colors.glassBase,
  color: THEME.colors.textSecondary,
};

export const dangerButtonStyle = {
  ...modalButtonStyle,
  backgroundColor: `${THEME.colors.statusRed}33`,
  color: THEME.colors.statusRed,
};

Modal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  title: PropTypes.string,
  children: PropTypes.node,
  footer: PropTypes.node,
};
