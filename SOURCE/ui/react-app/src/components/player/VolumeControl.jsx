/**
 * VolumeControl — Volume icon + range slider.
 *
 * Optimistic + throttled (#2772): the thumb tracks the drag off local state
 * immediately (never waits on the server), while the outbound onVolumeChange
 * (-> api.setVolume POST /v1/volume) is trailing-throttled so a fast drag
 * emits a bounded number of requests instead of one per input tick. Local
 * state only reconciles with the server-truth `volume` prop while the user
 * isn't actively dragging, which is what stops the old bug where the thumb
 * snapped back toward the last server value between drags.
 *
 * The drag guard POSTPONES reconciliation; it must never lose it. A server
 * value that lands mid-drag is remembered and applied the moment the drag
 * settles, because the reconcile effect keys on the `volume` prop and so would
 * otherwise never see that value again -- leaving the thumb on a number nobody
 * set, unable to correct itself. Only news that arrived after our own last
 * commit is adopted, so the echo of a state we already overrode still cannot
 * bounce the thumb.
 */
import PropTypes from 'prop-types';
import { useCallback, useEffect, useRef, useState } from 'react';
import { VolumeIcon } from './TransportIcons';
import styles from './VolumeControl.module.css';

// Trailing-edge throttle window for the outbound commit.
const COMMIT_THROTTLE_MS = 50;
// Safety net: if the browser never fires pointerup/keyup/blur for this drag
// (e.g. the pointer is released outside the window), stop treating the
// slider as "mid-drag" after this many idle ms so it can't wedge stale and
// permanently ignore server reconciliation.
const DRAG_IDLE_RELEASE_MS = 300;

const VolumeControl = ({ volume, onVolumeChange }) => {
  const [localVolume, setLocalVolume] = useState(volume);
  const isDraggingRef = useRef(false);
  const pendingRef = useRef(null);
  const commitTimerRef = useRef(null);
  const idleReleaseTimerRef = useRef(null);
  const onVolumeChangeRef = useRef(onVolumeChange);
  onVolumeChangeRef.current = onVolumeChange;
  // Which came last: the server telling us something, or us telling the
  // server something. That ordering is what separates "the server has news we
  // skipped while guarding the drag" from "the server has not echoed our own
  // commit yet". A shared counter rather than a clock: two events in the same
  // millisecond are common (and Date.now() does not move at all under a test's
  // fake timers), and a tie here reads as "no news", which is the bug.
  const eventSeqRef = useRef(0);
  const serverVolumeRef = useRef(volume);
  const serverSeqRef = useRef(0);
  const commitSeqRef = useRef(0);

  // Reconcile with server truth whenever the caller isn't mid-drag — this
  // is what lets server-driven changes (another client, a mute toggle)
  // through immediately, while never stomping an in-flight local drag.
  useEffect(() => {
    serverVolumeRef.current = volume;
    eventSeqRef.current += 1;
    serverSeqRef.current = eventSeqRef.current;
    if (!isDraggingRef.current) {
      setLocalVolume(volume);
    }
  }, [volume]);

  useEffect(() => () => {
    if (commitTimerRef.current) clearTimeout(commitTimerRef.current);
    if (idleReleaseTimerRef.current) clearTimeout(idleReleaseTimerRef.current);
  }, []);

  const flushPending = useCallback(() => {
    if (commitTimerRef.current) {
      clearTimeout(commitTimerRef.current);
      commitTimerRef.current = null;
    }
    if (pendingRef.current !== null) {
      const value = pendingRef.current;
      pendingRef.current = null;
      eventSeqRef.current += 1;
      commitSeqRef.current = eventSeqRef.current;
      onVolumeChangeRef.current(value);
    }
  }, []);

  // Called on drag end (pointerup/keyup/blur) and by the idle-release
  // safety net: stop guarding local state and commit whatever is pending
  // right away, so the final value is always applied without waiting out
  // the throttle window.
  const settleDrag = useCallback(() => {
    isDraggingRef.current = false;
    if (idleReleaseTimerRef.current) {
      clearTimeout(idleReleaseTimerRef.current);
      idleReleaseTimerRef.current = null;
    }
    // Pick up anything the server said WHILE the guard was up. The reconcile
    // effect above is keyed on the `volume` prop, so a server change that
    // landed mid-drag was not merely postponed -- it was dropped for good,
    // because the prop already holds that value and will never "change" to it
    // again. The slider then showed a number nobody had set and could not
    // correct itself until some third party moved the volume to yet another
    // value. That is how a Playwright chaos probe watched a `/v1/volume`
    // write of 42 leave the thumb sitting on 11 for a full 15 seconds.
    //
    // Only adopt news that arrived AFTER our own last commit. An older server
    // value is just the echo of the state we already overrode, and snapping
    // back to it is the thumb-bounce this component's drag guard exists to
    // prevent.
    if (serverSeqRef.current > commitSeqRef.current) {
      setLocalVolume(serverVolumeRef.current);
    }
    flushPending();
  }, [flushPending]);

  const handleChange = (e) => {
    const value = parseInt(e.target.value, 10);
    isDraggingRef.current = true;
    setLocalVolume(value);

    pendingRef.current = value;
    if (!commitTimerRef.current) {
      commitTimerRef.current = setTimeout(() => {
        commitTimerRef.current = null;
        if (pendingRef.current !== null) {
          const finalValue = pendingRef.current;
          pendingRef.current = null;
          // Same bookkeeping as flushPending: BOTH commit paths have to take a
          // sequence number, or settleDrag mistakes our own un-echoed write
          // for fresh server news and bounces the thumb.
          eventSeqRef.current += 1;
          commitSeqRef.current = eventSeqRef.current;
          onVolumeChangeRef.current(finalValue);
        }
      }, COMMIT_THROTTLE_MS);
    }

    if (idleReleaseTimerRef.current) clearTimeout(idleReleaseTimerRef.current);
    idleReleaseTimerRef.current = setTimeout(settleDrag, DRAG_IDLE_RELEASE_MS);
  };

  return (
    <div className={`volume-control ${styles.container}`}>
      <VolumeIcon level={localVolume} />
      <input
        type="range"
        role="slider"
        min="0"
        max="100"
        value={localVolume}
        onChange={handleChange}
        onPointerUp={settleDrag}
        onKeyUp={settleDrag}
        onBlur={settleDrag}
        aria-label="Volume"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={localVolume}
        className={`volume-slider ${styles.slider}`}
        style={{ '--vol-fill': `${localVolume}%` }}
      />
    </div>
  );
};

VolumeControl.propTypes = {
  volume: PropTypes.number.isRequired,
  onVolumeChange: PropTypes.func.isRequired,
};

export default VolumeControl;
