import { useEffect, useRef, useState } from 'react';
import { authFetch } from './useViolaApi';

const RETRY_MESSAGE = "Couldn't confirm your reply. Your answer is saved here. Try again or take over.";

// Keep the draft and request state above the popup/inline presentations so a
// stage change cannot lose an answer or submit the same question twice.
export default function useCallConsultationReply(consultation, setConsultation) {
  const [state, setState] = useState(null);
  const requests = useRef(new WeakSet());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  // Each WS payload is a distinct consultation, even when a call asks the
  // same question again. The reply API currently has no question/token field.
  const current = state?.consultation === consultation ? state : null;
  const answer = current?.answer || '';
  const pending = current?.pending || false;
  const error = current?.error || '';

  const setAnswer = (value) => {
    if (!consultation || requests.current.has(consultation)) return;
    setState({ consultation, answer: value, pending: false, error: '' });
  };

  const submit = async () => {
    const callId = consultation?.call_id || consultation?.id;
    if (!callId || !answer.trim() || requests.current.has(consultation)) return;
    requests.current.add(consultation);
    setState({ consultation, answer, pending: true, error: '' });
    let accepted = false;
    let message = RETRY_MESSAGE;
    try {
      const response = await authFetch(`/v1/phone/call/${encodeURIComponent(callId)}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ answer: answer.trim() }),
      });
      if (response.ok) {
        const body = await response.json();
        accepted = body?.ok === true;
      } else if (response.status === 404) {
        message = 'This question is no longer pending. Your answer is saved here. You can dismiss it or take over.';
      } else if (response.status === 401 || response.status === 403) {
        message = 'Your reply was not authorized. Your answer is saved here. Check your sign-in before trying again.';
      }
    } catch {
      // A lost or malformed response cannot confirm that the reply was accepted.
    }
    if (!accepted) requests.current.delete(consultation);
    if (!mounted.current) return;
    setState(previous => previous?.consultation === consultation
      ? { ...previous, answer: accepted ? '' : previous.answer, pending: false, error: accepted ? '' : message }
      : previous);
    if (accepted) {
      // The next call/question may have arrived while the request was in flight.
      setConsultation(previous => previous === consultation ? null : previous);
    }
  };

  return { answer, setAnswer, pending, error, submit };
}
