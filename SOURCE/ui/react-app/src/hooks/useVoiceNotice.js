import { useEffect, useRef, useState } from 'react';

/** How long a voice failure stays up with nothing else happening. */
export const VOICE_NOTICE_TTL_MS = 10000;

/**
 * The voice failure worth showing right now, or null.
 *
 * `voice.error` only clears when a new recording starts, so a notice like
 * "didn't hear anything" outlived everything typed or woken after it. This
 * makes it a notice about one turn: it ends on a timeout, when a reply lands,
 * and when the next turn starts by any route (mic, wake word, typing).
 */
export function useVoiceNotice(voice, lastResponse, isCommandLoading = false) {
  const { error, isRecording, isProcessing, clearError } = voice;
  const [notice, setNotice] = useState(null);
  const clearErrorRef = useRef(clearError);
  clearErrorRef.current = clearError;
  const replyAtErrorRef = useRef(lastResponse);

  useEffect(() => {
    replyAtErrorRef.current = lastResponse;
    setNotice(error || null);
    if (!error) return undefined;
    const timer = setTimeout(() => {
      setNotice(null);
      clearErrorRef.current?.();
    }, VOICE_NOTICE_TTL_MS);
    return () => clearTimeout(timer);
    // lastResponse is read, not watched: a reply must not re-arm the timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [error]);

  const nextTurnStarted = Boolean(isRecording || isProcessing || isCommandLoading);
  const replyLanded = lastResponse !== replyAtErrorRef.current;
  useEffect(() => {
    if (!notice || (!nextTurnStarted && !replyLanded)) return;
    setNotice(null);
    clearErrorRef.current?.();
  }, [notice, nextTurnStarted, replyLanded]);

  return nextTurnStarted || replyLanded ? null : notice;
}

export default useVoiceNotice;
