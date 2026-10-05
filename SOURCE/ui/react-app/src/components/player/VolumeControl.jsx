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
 * value that lands mid-drag is remembered and applied once the drag and any
 * admitted native request settle. The effect keys on the `volume` prop and so would
 * otherwise never see that value again -- leaving the thumb on a number nobody
 * set, unable to correct itself. Only news that arrived after our own last
 * commit is adopted, so the echo of a state we already overrode still cannot
 * bounce the thumb.
 */
import PropTypes from 'prop-types';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { VolumeIcon } from './TransportIcons';
import styles from './VolumeControl.module.css';

// Trailing-edge throttle window for the outbound commit.
const COMMIT_THROTTLE_MS = 50;
// Safety net: if the browser never fires pointerup/keyup/blur for this drag
// (e.g. the pointer is released outside the window), stop treating the
// slider as "mid-drag" after this many idle ms so it can't wedge stale and
// permanently ignore server reconciliation.
const DRAG_IDLE_RELEASE_MS = 300;
// Retire UI ownership after uncertainty; this cannot cancel backend audio work.
const ACK_TIMEOUT_MS = 15000;

const VolumeControl = ({ volume, onVolumeChange, onVolumeError }) => {
  const [localVolume, setLocalVolume] = useState(volume);
  const [ackPending, setAckPending] = useState(false);
  const [ackUncertain, setAckUncertain] = useState(false);
  const sessionRef = useRef(null);
  const inputVersionRef = useRef(0);
  const confirmedVolumeRef = useRef(volume);
  const onVolumeErrorRef = useRef(onVolumeError);
  onVolumeErrorRef.current = onVolumeError;
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
    confirmedVolumeRef.current = volume;
    eventSeqRef.current += 1;
    serverSeqRef.current = eventSeqRef.current;
    if (!isDraggingRef.current && !sessionRef.current?.request && !sessionRef.current?.queued) {
      setLocalVolume(volume);
    }
  }, [volume]);

  useLayoutEffect(() => {
    const session = { active: true, request: null, queued: null };
    sessionRef.current = session;
    return () => {
      session.active = false;
      if (session.request) clearTimeout(session.request.timer);
      session.queued = null;
      if (commitTimerRef.current) clearTimeout(commitTimerRef.current);
      if (idleReleaseTimerRef.current) clearTimeout(idleReleaseTimerRef.current);
    };
  }, []);

  const sendVolume = useCallback(function send(value, version) {
    const session = sessionRef.current;
    if (!session?.active) return;
    if (session.request) {
      // Coalesce intermediate drag ticks while preserving the latest intent.
      session.queued = { value, version };
      return;
    }
    const request = { value, version, seq: ++eventSeqRef.current, deadline: Infinity, timer: null };
    commitSeqRef.current = request.seq;
    session.request = request;
    const finish = (result, error = null, synchronous = false) => {
      if (!session.active || sessionRef.current !== session || session.request !== request) return;
      const expired = performance.now() >= request.deadline;
      const accepted = !error && !expired && (
        (synchronous && result === undefined)
        || (result?.ok === true && Number.isFinite(result.volume) && result.volume >= 0 && result.volume <= 100)
      );
      clearTimeout(request.timer);
      session.request = null;
      if (accepted && serverSeqRef.current <= request.seq) {
        confirmedVolumeRef.current = result?.volume ?? value;
      }
      const current = version === inputVersionRef.current;
      if (current) {
        setAckUncertain(expired);
        if (!accepted && !expired) {
          onVolumeErrorRef.current?.(error || { status: 500, code: 'volume_unconfirmed' });
        }
      }
      const queued = session.queued;
      session.queued = null;
      if (pendingRef.current === null && !queued) setLocalVolume(confirmedVolumeRef.current);
      setAckPending(Boolean(queued));
      if (queued) send(queued.value, queued.version);
    };
    try {
      const result = onVolumeChangeRef.current(value);
      if (result && typeof result.then === 'function') {
        request.deadline = performance.now() + ACK_TIMEOUT_MS;
        request.timer = setTimeout(() => finish(null), ACK_TIMEOUT_MS);
        setAckPending(true);
        Promise.resolve(result).then(value => finish(value), error => finish(null, error));
      } else {
        // Existing synchronous (iframe) callbacks retain their own contract.
        finish(result, null, true);
      }
    } catch (error) {
      finish(null, error);
    }
  }, []);

  const flushPending = useCallback(() => {
    if (commitTimerRef.current) {
      clearTimeout(commitTimerRef.current);
      commitTimerRef.current = null;
    }
    if (pendingRef.current !== null) {
      const value = pendingRef.current;
      pendingRef.current = null;
      sendVolume(value.value, value.version);
    }
  }, [sendVolume]);

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
    // Admit the last drag before deciding whether the old prop is news.
    flushPending();
    if (!sessionRef.current?.request && !sessionRef.current?.queued
        && serverSeqRef.current > commitSeqRef.current) {
      setLocalVolume(serverVolumeRef.current);
    }
  }, [flushPending]);

  const handleChange = (e) => {
    const value = parseInt(e.target.value, 10);
    isDraggingRef.current = true;
    setLocalVolume(value);

    setAckUncertain(false);
    pendingRef.current = { value, version: ++inputVersionRef.current };
    if (!commitTimerRef.current) {
      commitTimerRef.current = setTimeout(flushPending, COMMIT_THROTTLE_MS);
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
        aria-busy={ackPending}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={localVolume}
        className={`volume-slider ${styles.slider}`}
        style={{ '--vol-fill': `${localVolume}%` }}
      />
      {ackPending && <span role="status">Changing volume…</span>}
      {!ackPending && ackUncertain && <span role="status">Volume change not confirmed. Check the level and try again.</span>}
    </div>
  );
};

VolumeControl.propTypes = {
  volume: PropTypes.number.isRequired,
  onVolumeChange: PropTypes.func.isRequired,
  onVolumeError: PropTypes.func,
};

export default VolumeControl;
