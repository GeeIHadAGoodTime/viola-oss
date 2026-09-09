import React, { useState, useEffect, useCallback } from 'react';
import PropTypes from 'prop-types';

/**
 * Unique ID for the keyframes style element
 */
const KEYFRAMES_ID = 'onboarding-highlight-keyframes';

/**
 * CSS keyframes for the pulse animation.
 * Injected into the document head once when needed.
 */
const PULSE_KEYFRAMES = `
@keyframes onboarding-pulse {
  0%, 100% { opacity: 0.8; transform: scale(1); }
  50% { opacity: 1; transform: scale(1.05); }
}
`;

/**
 * Inject keyframes into document head if not already present.
 */
function ensureKeyframes() {
  if (typeof document === 'undefined') {
    return;
  }
  if (!document.getElementById(KEYFRAMES_ID)) {
    const style = document.createElement('style');
    style.id = KEYFRAMES_ID;
    style.textContent = PULSE_KEYFRAMES;
    document.head.appendChild(style);
  }
}

/**
 * OnboardingHighlight - Renders a glowing highlight overlay on UI elements during voice onboarding.
 *
 * This component finds a target element by ID, calculates its bounding rect,
 * and positions a fixed overlay with a purple glow and pulse animation.
 * The overlay uses pointer-events: none so clicks pass through to the underlying element.
 *
 * @param {string} targetId - The ID of the DOM element to highlight
 * @param {boolean} active - Whether the highlight should be visible
 */
export default function OnboardingHighlight({ targetId, active }) {
  const [rect, setRect] = useState(null);

  /**
   * Update the bounding rect of the target element.
   */
  const updateRect = useCallback(() => {
    if (!targetId) {
      setRect(null);
      return;
    }
    const element = document.getElementById(targetId);
    if (!element) {
      setRect(null);
      return;
    }
    const boundingRect = element.getBoundingClientRect();
    setRect({
      top: boundingRect.top,
      left: boundingRect.left,
      width: boundingRect.width,
      height: boundingRect.height,
    });
  }, [targetId]);

  /**
   * Set up effects: inject keyframes, calculate initial rect, handle resize.
   *
   * While active, the real target element is lifted above the onboarding
   * backdrop (which sits at z-index 90 with a heavy blur). Without this the
   * highlighted control renders *behind* the blur — dim and, depending on what
   * else is layered in, awkward to click. Raising it to z-index 95 with
   * pointer-events:auto guarantees the highlighted control both SHOWS crisply
   * and RECEIVES real clicks. The element's own inline style is captured and
   * restored on cleanup so nothing leaks after onboarding.
   */
  useEffect(() => {
    if (!active) {
      return undefined;
    }

    // Ensure keyframes are available
    ensureKeyframes();

    // Calculate initial position
    updateRect();

    // Lift the real target above the onboarding backdrop.
    const target = targetId ? document.getElementById(targetId) : null;
    let restore = null;
    if (target) {
      const prevPosition = target.style.position;
      const prevZIndex = target.style.zIndex;
      const prevPointerEvents = target.style.pointerEvents;
      const computedPosition = window.getComputedStyle(target).position;
      if (computedPosition === 'static') {
        target.style.position = 'relative';
      }
      target.style.zIndex = '95';
      target.style.pointerEvents = 'auto';
      restore = () => {
        target.style.position = prevPosition;
        target.style.zIndex = prevZIndex;
        target.style.pointerEvents = prevPointerEvents;
      };
    }

    // Recalculate on window resize
    window.addEventListener('resize', updateRect);

    // Also recalculate on scroll (in case element is inside scrollable container)
    window.addEventListener('scroll', updateRect, true);

    return () => {
      window.removeEventListener('resize', updateRect);
      window.removeEventListener('scroll', updateRect, true);
      if (restore) restore();
    };
  }, [active, updateRect, targetId]);

  /**
   * Re-calculate rect when targetId changes while active.
   */
  useEffect(() => {
    if (active) {
      updateRect();
    }
  }, [targetId, active, updateRect]);

  // Graceful degradation: return null if not active or element not found
  if (!active || !rect) {
    return null;
  }

  // Warm bronze glow — matches Viola's mahogany/bronze onboarding aesthetic.
  const glow = 'rgba(201, 138, 104, 0.7)';

  return (
    <div
      style={{
        position: 'fixed',
        top: rect.top,
        left: rect.left,
        width: rect.width,
        height: rect.height,
        pointerEvents: 'none',
        boxShadow: `0 0 22px 8px ${glow}`,
        borderRadius: '8px',
        animation: 'onboarding-pulse 1.5s ease-in-out infinite',
        zIndex: 9999,
        // Subtle border to match the glow
        border: `2px solid ${glow}`,
        boxSizing: 'border-box',
      }}
      aria-hidden="true"
      data-testid="onboarding-highlight"
    />
  );
}

OnboardingHighlight.propTypes = {
  /** The ID of the DOM element to highlight (null means no target) */
  targetId: PropTypes.string,
  /** Whether the highlight is active/visible */
  active: PropTypes.bool.isRequired,
};
