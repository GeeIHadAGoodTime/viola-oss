import { useCallback, useEffect, useRef } from 'react';

/**
 * Press-and-hold gesture for push-to-talk buttons, built on Pointer Events.
 *
 * The old buttons wired mouse and touch handlers separately and only ended a
 * hold on mouseup / mouseleave / touchend. On a touch screen (a Surface in
 * tablet mode) that broke in three ways:
 *   - a small finger slide started a scroll or drag, the browser fired
 *     pointercancel/touchcancel instead of touchend, and the hold never ended
 *     (or mouseleave ended it early when the finger "slid off" the button);
 *   - React touch listeners are passive, so preventDefault() in touchstart
 *     could not stop the emulated mousedown/mouseup that follow a tap, which
 *     started and ended a second recording;
 *   - a long press raised the context menu, which also cancels the gesture.
 *
 * This hook owns one pointer at a time, captures it so the hold survives the
 * finger drifting off the button, and ends the hold on pointerup,
 * pointercancel or lostpointercapture, whichever comes first, exactly once.
 * Pair it with `touch-action: none` on the button so the browser never turns
 * the press into a scroll or pan.
 */
function isActivePointer(activeRef, event) {
  const active = activeRef.current;
  return Boolean(active) && active.pointerId === event.pointerId;
}

export function usePressAndHold({ onStart, onEnd, disabled = false } = {}) {
  const activeRef = useRef(null);
  const onStartRef = useRef(onStart);
  const onEndRef = useRef(onEnd);
  onStartRef.current = onStart;
  onEndRef.current = onEnd;

  const endHold = useCallback((reason) => {
    const active = activeRef.current;
    if (!active) return false;
    activeRef.current = null;
    try {
      if (active.target?.hasPointerCapture?.(active.pointerId)) {
        active.target.releasePointerCapture(active.pointerId);
      }
    } catch {
      // The element may already be detached; the hold still ends below.
    }
    onEndRef.current?.(reason);
    return true;
  }, []);

  // A hold must never outlive the button (mode switch, modal, re-render that
  // swaps the element): end it so the microphone is not left recording.
  useEffect(() => () => { endHold('unmount'); }, [endHold]);

  const onPointerDown = useCallback((event) => {
    if (disabled) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    // A second finger while one is already holding is ignored.
    if (activeRef.current) return;
    event.preventDefault();
    event.stopPropagation();
    const target = event.currentTarget;
    activeRef.current = { pointerId: event.pointerId, target };
    try {
      target?.setPointerCapture?.(event.pointerId);
    } catch {
      // Capture is best effort; pointerup/pointercancel still end the hold.
    }
    onStartRef.current?.();
  }, [disabled]);

  const onPointerUp = useCallback((event) => {
    if (!isActivePointer(activeRef, event)) return;
    event.preventDefault();
    event.stopPropagation();
    endHold('release');
  }, [endHold]);

  const onPointerCancel = useCallback((event) => {
    if (!isActivePointer(activeRef, event)) return;
    endHold('cancel');
  }, [endHold]);

  const onLostPointerCapture = useCallback((event) => {
    if (!isActivePointer(activeRef, event)) return;
    endHold('lost_capture');
  }, [endHold]);

  const preventDefault = useCallback((event) => {
    event.preventDefault();
  }, []);

  return {
    onPointerDown,
    onPointerUp,
    onPointerCancel,
    onLostPointerCapture,
    // A long press would otherwise open the context menu (and cancel the
    // gesture); a drag would start a native drag of the button image.
    onContextMenu: preventDefault,
    onDragStart: preventDefault,
  };
}

export default usePressAndHold;
