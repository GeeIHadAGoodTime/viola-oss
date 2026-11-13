import { describe, expect, it, vi } from 'vitest';
import { agentResultView } from './SmartDisplay';
import { SPOKEN_BUDGET_CHARS } from './hooks/usePacedReply';

vi.mock('./sentryClient', () => ({ syncSentrySettings: vi.fn(), openSentryUserFeedback: vi.fn() }));

// Build-11 laptop report: a 2,000-word background-agent essay was dumped into the
// small stage text area. A long result goes on the big card like a long answer.
describe('long answers', () => {
  it('puts a long background-agent result on the big card with a short response line', () => {
    const essay = 'The cello is a bowed string instrument. '.repeat(60);
    const view = agentResultView(essay, false);
    expect(view.card).toEqual({ type: 'detail', body: essay, long_answer: true });
    expect(view.line.length).toBeLessThanOrEqual(163);
  });

  it('keeps a short result in the response line without a card', () => {
    const view = agentResultView('Your report is saved in Documents.', false);
    expect(view.card).toBeNull();
    expect(view.line).toBe('Your report is saved in Documents.');
  });

  it('paces the stage only as far as a long answer is spoken (answer_stream LONG_ANSWER_CHARS)', () => {
    expect(SPOKEN_BUDGET_CHARS).toBe(200);
  });
});
