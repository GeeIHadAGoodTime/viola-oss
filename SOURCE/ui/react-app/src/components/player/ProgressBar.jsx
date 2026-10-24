/**
 * ProgressBar — Tap- or drag-to-seek track progress bar.
 *
 * A press on the track captures the pointer and the fill follows the finger
 * (or mouse) while it moves; the seek is sent once, on release. A drag that
 * the browser cancels (pointercancel) seeks nowhere. Keyboard / assistive
 * "click" activations still seek through onClick.
 */
import { useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './ProgressBar.module.css';

function fractionAt(event, element) {
  const rect = element.getBoundingClientRect();
  if (!rect.width) return 0;
  return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
}

const ProgressBar = ({ position, duration, progress, onSeek }) => {
  const [scrubFraction, setScrubFraction] = useState(null);
  const scrubRef = useRef(null);
  const suppressClickRef = useRef(false);

  const seekTo = (fraction) => {
    if (duration > 0) onSeek(Math.floor(fraction * duration));
  };

  const handlePointerDown = (e) => {
    if (!(duration > 0) || (e.pointerType === 'mouse' && e.button !== 0)) return;
    const fraction = fractionAt(e, e.currentTarget);
    scrubRef.current = { pointerId: e.pointerId, fraction };
    try { e.currentTarget.setPointerCapture?.(e.pointerId); } catch { /* not capturable */ }
    setScrubFraction(fraction);
  };

  const handlePointerMove = (e) => {
    const scrub = scrubRef.current;
    if (!scrub || scrub.pointerId !== e.pointerId) return;
    scrub.fraction = fractionAt(e, e.currentTarget);
    setScrubFraction(scrub.fraction);
  };

  const endScrub = (e, commit) => {
    const scrub = scrubRef.current;
    if (!scrub || scrub.pointerId !== e.pointerId) return;
    scrubRef.current = null;
    setScrubFraction(null);
    if (commit) {
      suppressClickRef.current = true;
      seekTo(fractionAt(e, e.currentTarget));
    }
  };

  const handleClick = (e) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    seekTo(fractionAt(e, e.currentTarget));
  };

  const shownProgress = scrubFraction === null ? progress : scrubFraction * 100;

  return (
    <div className={`progress-bar-container ${styles.container}`}>
      <div
        role="progressbar"
        aria-valuenow={position || 0}
        aria-valuemin={0}
        aria-valuemax={duration || 100}
        aria-label="Track progress"
        className={`progress-bar ${styles.track}`}
        data-scrubbing={scrubFraction === null ? undefined : 'true'}
        style={{ backgroundColor: THEME.colors.glassHover }}
        onClick={handleClick}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={(e) => endScrub(e, true)}
        onPointerCancel={(e) => endScrub(e, false)}
        onLostPointerCapture={(e) => endScrub(e, false)}
      >
        <div
          className={`progress-bar-fill ${styles.fill}`}
          style={{
            width: `${shownProgress}%`,
            backgroundColor: THEME.colors.textBright,
          }}
        />
      </div>
    </div>
  );
};

ProgressBar.propTypes = {
  position: PropTypes.number,
  duration: PropTypes.number,
  progress: PropTypes.number.isRequired,
  onSeek: PropTypes.func.isRequired,
};

export default ProgressBar;
