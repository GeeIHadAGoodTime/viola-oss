/**
 * The voice-stream client rebuilds a narrow envelope from the `command_result`
 * frame rather than forwarding it whole, so anything the rebuild drops is
 * invisible to the response area. A spoken cap denial has nothing to tap, which
 * makes carrying the cap state through this seam the whole affordance on the
 * voice surface (candidate C-077).
 *
 * This locks the SHAPE useVoiceWs builds against the recogniser that consumes
 * it, so a future narrowing of either side fails here instead of silently
 * removing the upgrade route from browser and spoke voice turns.
 */
import { describe, it, expect } from 'vitest';
import { extractCapDenial } from './capDenial';

const CAP_STATE = {
  plan: 'free',
  period: 'weekly',
  spent_cents: 600,
  limit_cents: 600,
  resets_at: '2026-08-01T00:00:00+00:00',
};

/** Mirrors the envelope useVoiceWs builds for a `command_result` frame. */
function envelopeFromFrame(msg) {
  const text = msg.transcript || '';
  const responseText = msg.response || '';
  const capState = msg.cap_state;
  return {
    ok: true,
    data: {
      text,
      transcript: text,
      message: responseText,
      response: responseText,
      ...(capState ? { cap_state: capState } : {}),
    },
  };
}

describe('voice command_result frame', () => {
  it('carries a cap denial through to the recogniser', () => {
    const envelope = envelopeFromFrame({
      type: 'command_result',
      transcript: 'what is the weather',
      response: "You've reached your weekly managed AI limit.",
      cap_state: CAP_STATE,
    });
    expect(extractCapDenial(envelope)).toEqual({
      plan: 'free',
      period: 'weekly',
      resetsAt: '2026-08-01T00:00:00+00:00',
    });
  });

  it('leaves an ordinary voice turn unmarked', () => {
    const envelope = envelopeFromFrame({
      type: 'command_result',
      transcript: 'play something',
      response: 'Playing music.',
    });
    expect(extractCapDenial(envelope)).toBeNull();
    expect('cap_state' in envelope.data).toBe(false);
  });

  it('treats a frame carrying an empty cap_state as an ordinary turn', () => {
    const envelope = envelopeFromFrame({
      type: 'command_result',
      transcript: 'play something',
      response: 'Playing music.',
      cap_state: {},
    });
    expect(extractCapDenial(envelope)).toBeNull();
  });
});
