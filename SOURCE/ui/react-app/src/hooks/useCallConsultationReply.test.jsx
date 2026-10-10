import PropTypes from 'prop-types';
import { useState } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '../test/test-utils';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { authFetch } from './useViolaApi';
import useCallConsultationReply from './useCallConsultationReply';
import CallConsultation from '../components/CallConsultation';
import PhoneCallPanel from '../components/PhoneCallPanel';

vi.mock('./useViolaApi', () => ({ authFetch: vi.fn() }));

const FIRST = { call_id: 'synthetic/call A', question: 'Approve option A?' };
const NEXT_QUESTION = { call_id: FIRST.call_id, question: 'Approve option B?' };
const NEXT_CALL = { call_id: 'synthetic-call-B', question: 'Approve option C?' };
const replyResponse = (status = 200, body = { ok: true }) => ({
  ok: status >= 200 && status < 300,
  status,
  json: vi.fn().mockResolvedValue(body),
});
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function Harness({ initialMode = 'popup', onTakeover = vi.fn() }) {
  const [consultation, setConsultation] = useState(FIRST);
  const [mode, setMode] = useState(initialMode);
  const reply = useCallConsultationReply(consultation, setConsultation);
  return (
    <>
      <button onClick={() => setConsultation(NEXT_QUESTION)}>Next question</button>
      <button onClick={() => setConsultation(NEXT_CALL)}>Next call</button>
      <button onClick={() => setConsultation({ ...FIRST })}>Repeat question</button>
      <button onClick={() => setConsultation(null)}>End synthetic call</button>
      <button onClick={() => setMode(previous => previous === 'popup' ? 'inline' : 'popup')}>Switch stage</button>
      <span data-testid="current-question">{consultation?.question || 'No consultation'}</span>
      {mode === 'popup' ? (
        <CallConsultation consultation={consultation} reply={reply} onTakeover={onTakeover} onDismiss={() => setConsultation(null)} />
      ) : (
        <PhoneCallPanel
          callId={consultation?.call_id || FIRST.call_id}
          onEndCall={vi.fn()}
          isListening={false}
          transcripts={[]}
          takeoverActive={false}
          onToggleListen={vi.fn()}
          onToggleTakeover={vi.fn()}
          onSendOperatorMessage={vi.fn()}
          activeConsultation={consultation}
          consultationReply={reply}
          onConsultationTakeover={onTakeover}
        />
      )}
    </>
  );
}

Harness.propTypes = {
  initialMode: PropTypes.oneOf(['popup', 'inline']),
  onTakeover: PropTypes.func,
};

const input = () => screen.getByPlaceholderText(/Type what Viola should say|Reply to the call agent/);
const send = () => within(input().closest('form')).getByRole('button', { name: /^(Send|Reply)$/ });
const typeAnswer = (answer = '  Yes, option A.  ') => fireEvent.change(input(), { target: { value: answer } });
const settle = async (request, response) => { await act(async () => request.resolve(response)); };

beforeEach(() => { vi.clearAllMocks(); });

for (const mode of ['popup', 'inline']) {
  describe(`${mode} consultation reply`, () => {
    it('keeps the draft pending, blocks duplicate submits, and clears only after a positive acknowledgement', async () => {
      const request = deferred();
      authFetch.mockReturnValue(request.promise);
      render(<Harness initialMode={mode} />);
      typeAnswer();
      const form = input().closest('form');
      fireEvent.submit(form);
      fireEvent.submit(form);
      expect(authFetch).toHaveBeenCalledTimes(1);
      expect(authFetch).toHaveBeenCalledWith('/v1/phone/call/synthetic%2Fcall%20A/reply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
        body: JSON.stringify({ answer: 'Yes, option A.' }),
      });
      expect(input()).toHaveValue('  Yes, option A.  ');
      expect(input()).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled();
      await settle(request, replyResponse());
      expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation');
      expect(screen.queryByPlaceholderText(/Type what Viola should say|Reply to the call agent/)).not.toBeInTheDocument();
    });

    it.each([
      ['network rejection', () => Promise.reject(new Error('synthetic offline'))],
      ['HTTP 500', () => Promise.resolve(replyResponse(500))],
      ['HTTP 401', () => Promise.resolve(replyResponse(401))],
      ['HTTP 403', () => Promise.resolve(replyResponse(403))],
      ['expired consultation', () => Promise.resolve(replyResponse(404))],
      ['application refusal', () => Promise.resolve(replyResponse(200, { ok: false }))],
      ['missing acknowledgement', () => Promise.resolve(replyResponse(200, {}))],
      ['non-boolean acknowledgement', () => Promise.resolve(replyResponse(200, { ok: 'true' }))],
      ['malformed JSON', () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error('bad JSON')) })],
    ])('retains the exact draft and allows an explicit retry after %s', async (_label, failure) => {
      authFetch.mockImplementationOnce(failure).mockResolvedValueOnce(replyResponse());
      render(<Harness initialMode={mode} />);
      typeAnswer();
      fireEvent.click(send());
      expect(await screen.findByRole('alert')).toHaveTextContent(/answer is saved here/);
      expect(input()).toHaveValue('  Yes, option A.  ');
      expect(send()).toBeEnabled();
      expect(screen.getByTestId('current-question')).toHaveTextContent(FIRST.question);
      fireEvent.click(send());
      await waitFor(() => expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation'));
      expect(authFetch).toHaveBeenCalledTimes(2);
    });

    it.each(['Next question', 'Next call', 'Repeat question'])('does not clear or alter the draft of a %s on stale success', async (replacement) => {
      const old = deferred();
      authFetch.mockReturnValueOnce(old.promise).mockResolvedValueOnce(replyResponse());
      render(<Harness initialMode={mode} />);
      typeAnswer('Original answer');
      fireEvent.click(send());
      fireEvent.click(screen.getByRole('button', { name: replacement }));
      expect(input()).toHaveValue('');
      typeAnswer('New answer');
      await settle(old, replyResponse());
      expect(input()).toHaveValue('New answer');
      expect(send()).toBeEnabled();
      fireEvent.click(send());
      await waitFor(() => expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation'));
      expect(authFetch).toHaveBeenCalledTimes(2);
    });

    it.each([200, 500])('does not attach an old %s response to the next question or unlock its request', async (oldStatus) => {
      const old = deferred();
      const next = deferred();
      authFetch.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
      render(<Harness initialMode={mode} />);
      typeAnswer('Original answer');
      fireEvent.click(send());
      fireEvent.click(screen.getByRole('button', { name: 'Next question' }));
      typeAnswer('New answer');
      fireEvent.click(send());
      await settle(old, replyResponse(oldStatus));
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(input()).toHaveValue('New answer');
      expect(input()).toBeDisabled();
      await settle(next, replyResponse());
      expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation');
    });

    it('preserves draft, pending state, failure and retry when switching presentations', async () => {
      const request = deferred();
      authFetch.mockReturnValueOnce(request.promise).mockResolvedValueOnce(replyResponse());
      render(<Harness initialMode={mode} />);
      typeAnswer();
      fireEvent.click(screen.getByRole('button', { name: 'Switch stage' }));
      expect(input()).toHaveValue('  Yes, option A.  ');
      fireEvent.click(send());
      fireEvent.click(screen.getByRole('button', { name: 'Switch stage' }));
      expect(input()).toBeDisabled();
      fireEvent.submit(input().closest('form'));
      expect(authFetch).toHaveBeenCalledTimes(1);
      await settle(request, replyResponse(500));
      expect(screen.getByRole('alert')).toHaveTextContent('Couldn\'t confirm');
      fireEvent.click(screen.getByRole('button', { name: 'Switch stage' }));
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(input()).toHaveValue('  Yes, option A.  ');
      fireEvent.click(send());
      await waitFor(() => expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation'));
    });

    it('does not resurrect an ended consultation after a failed reply', async () => {
      const request = deferred();
      authFetch.mockReturnValueOnce(request.promise);
      render(<Harness initialMode={mode} />);
      typeAnswer();
      fireEvent.click(send());
      fireEvent.click(screen.getByRole('button', { name: 'End synthetic call' }));
      await settle(request, replyResponse(500));
      expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation');
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('keeps takeover available while a reply is pending', async () => {
      const request = deferred();
      const onTakeover = vi.fn();
      authFetch.mockReturnValueOnce(request.promise);
      render(<Harness initialMode={mode} onTakeover={onTakeover} />);
      typeAnswer();
      fireEvent.click(send());
      fireEvent.click(screen.getAllByRole('button', { name: /^Take over$/i })[0]);
      expect(onTakeover).toHaveBeenCalledWith(FIRST.call_id);
      await settle(request, replyResponse());
    });

    it('ignores empty and whitespace-only drafts', () => {
      render(<Harness initialMode={mode} />);
      fireEvent.submit(input().closest('form'));
      typeAnswer('   ');
      fireEvent.submit(input().closest('form'));
      expect(authFetch).not.toHaveBeenCalled();
    });
  });
}

it('keeps dismissal available and does not dismiss a new consultation when the old reply succeeds', async () => {
  const request = deferred();
  authFetch.mockReturnValueOnce(request.promise);
  render(<Harness />);
  typeAnswer();
  fireEvent.click(send());
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss consultation' }));
  expect(screen.getByTestId('current-question')).toHaveTextContent('No consultation');
  fireEvent.click(screen.getByRole('button', { name: 'Next question' }));
  typeAnswer('Keep this draft');
  await settle(request, replyResponse());
  expect(input()).toHaveValue('Keep this draft');
  expect(screen.getByTestId('current-question')).toHaveTextContent(NEXT_QUESTION.question);
});

it('settles safely after the owning display unmounts', async () => {
  const request = deferred();
  authFetch.mockReturnValueOnce(request.promise);
  const { unmount } = render(<Harness />);
  typeAnswer();
  fireEvent.click(send());
  unmount();
  await settle(request, replyResponse());
  expect(authFetch).toHaveBeenCalledTimes(1);
});


it('acknowledges one successful consultation only once, even if an old submit callback is retained', async () => {
  const setConsultation = vi.fn();
  authFetch.mockResolvedValue(replyResponse());
  const { result } = renderHook(() => useCallConsultationReply(FIRST, setConsultation));
  act(() => result.current.setAnswer('One answer'));
  const submit = result.current.submit;
  await act(async () => { await submit(); await submit(); });
  expect(authFetch).toHaveBeenCalledTimes(1);
  expect(setConsultation).toHaveBeenCalledTimes(1);
  const clearMatching = setConsultation.mock.calls[0][0];
  expect(clearMatching(FIRST)).toBeNull();
  expect(clearMatching(NEXT_QUESTION)).toBe(NEXT_QUESTION);
  expect(clearMatching(NEXT_CALL)).toBe(NEXT_CALL);
  expect(clearMatching(null)).toBeNull();
});

describe('server question correlation', () => {
  const question = { ...FIRST, consultation_id: 'synthetic-question-a' };
  const nextQuestion = { ...FIRST, consultation_id: 'synthetic-question-b' };

  it('echoes the captured server question ID alongside the answer', async () => {
    authFetch.mockResolvedValue(replyResponse());
    const { result } = renderHook(() => useCallConsultationReply(question, vi.fn()));
    act(() => result.current.setAnswer('  Correlated answer  '));
    await act(async () => { await result.current.submit(); });
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({
      answer: 'Correlated answer', consultation_id: question.consultation_id,
    });
  });

  it('keeps the draft with an expired-question explanation after HTTP 409', async () => {
    const setConsultation = vi.fn();
    authFetch.mockResolvedValue(replyResponse(409, { ok: false }));
    const { result } = renderHook(() => useCallConsultationReply(question, setConsultation));
    act(() => result.current.setAnswer('  Preserve this answer  '));
    await act(async () => { await result.current.submit(); });
    expect(result.current.answer).toBe('  Preserve this answer  ');
    expect(result.current.error).toMatch(/question is no longer pending/);
    expect(result.current.pending).toBe(false);
    expect(setConsultation).not.toHaveBeenCalled();
  });

  it('keeps distinct IDs for identical wording and ignores a stale 409 while a newer reply is pending', async () => {
    const old = deferred();
    const next = deferred();
    authFetch.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
    const setConsultation = vi.fn();
    const { result, rerender } = renderHook(({ value }) => useCallConsultationReply(value, setConsultation), {
      initialProps: { value: question },
    });
    act(() => result.current.setAnswer('Old answer'));
    act(() => { void result.current.submit(); });
    rerender({ value: nextQuestion });
    act(() => result.current.setAnswer('New answer'));
    act(() => { void result.current.submit(); });
    expect(authFetch.mock.calls.map(([, options]) => JSON.parse(options.body))).toEqual([
      { answer: 'Old answer', consultation_id: question.consultation_id },
      { answer: 'New answer', consultation_id: nextQuestion.consultation_id },
    ]);
    await settle(old, replyResponse(409, { ok: false }));
    expect(result.current.answer).toBe('New answer');
    expect(result.current.pending).toBe(true);
    expect(result.current.error).toBe('');
    expect(setConsultation).not.toHaveBeenCalled();
    await settle(next, replyResponse());
    const clearMatching = setConsultation.mock.calls[0][0];
    expect(clearMatching(question)).toBe(question);
    expect(clearMatching(nextQuestion)).toBeNull();
  });

  it('retries an unconfirmed answer with its original question ID', async () => {
    authFetch.mockRejectedValueOnce(new Error('Synthetic lost response')).mockResolvedValueOnce(replyResponse(409));
    const { result } = renderHook(() => useCallConsultationReply(question, vi.fn()));
    act(() => result.current.setAnswer('Same answer'));
    await act(async () => { await result.current.submit(); });
    await act(async () => { await result.current.submit(); });
    expect(authFetch.mock.calls.map(([, options]) => JSON.parse(options.body).consultation_id)).toEqual([
      question.consultation_id, question.consultation_id,
    ]);
    expect(result.current.answer).toBe('Same answer');
    expect(result.current.error).toMatch(/question is no longer pending/);
  });

  it('does not silently omit an explicitly malformed event ID', async () => {
    authFetch.mockResolvedValue(replyResponse(400));
    const invalid = { ...question, consultation_id: null };
    const { result } = renderHook(() => useCallConsultationReply(invalid, vi.fn()));
    act(() => result.current.setAnswer('Do not downgrade'));
    await act(async () => { await result.current.submit(); });
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({
      answer: 'Do not downgrade', consultation_id: null,
    });
    expect(result.current.answer).toBe('Do not downgrade');
    expect(result.current.error).toMatch(/Couldn.t confirm/);
  });
});
