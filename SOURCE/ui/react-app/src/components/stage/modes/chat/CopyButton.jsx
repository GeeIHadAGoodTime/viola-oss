import { useLayoutEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';

const COPIED_FEEDBACK_MS = 1100;

export default function CopyButton({ text }) {
  const [status, setStatus] = useState('idle');
  const attemptRef = useRef(null);
  const resetTimerRef = useRef(null);

  useLayoutEffect(() => {
    setStatus('idle');
    return () => {
      // A changed message or unmounted button no longer owns async feedback.
      attemptRef.current = null;
      window.clearTimeout(resetTimerRef.current);
    };
  }, [text]);

  const handleCopy = async () => {
    if (attemptRef.current?.pending) return;
    const attempt = { pending: true };
    attemptRef.current = attempt;
    window.clearTimeout(resetTimerRef.current);
    setStatus('copying');
    try {
      const clipboard = navigator.clipboard;
      if (typeof clipboard?.writeText !== 'function') throw new Error('Clipboard unavailable');
      // Start the write in the click handler to preserve browser user activation.
      await clipboard.writeText(text);
      if (attemptRef.current !== attempt) return;
      attempt.pending = false;
      setStatus('copied');
      resetTimerRef.current = window.setTimeout(() => {
        if (attemptRef.current === attempt) setStatus('idle');
      }, COPIED_FEEDBACK_MS);
    } catch {
      if (attemptRef.current !== attempt) return;
      attempt.pending = false;
      setStatus('error');
    }
  };

  const label = { idle: 'Copy', copying: 'Copying…', copied: 'Copied', error: 'Copy failed. Retry' }[status];
  return (
    <button
      type="button"
      onClick={handleCopy}
      disabled={status === 'copying'}
      title={status === 'error' ? 'Try again, or select the text and copy it manually.' : undefined}
    >
      <span aria-live="polite" aria-atomic="true">{label}</span>
    </button>
  );
}

CopyButton.propTypes = {
  text: PropTypes.string.isRequired,
};
