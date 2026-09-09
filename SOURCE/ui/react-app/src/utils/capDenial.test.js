import { describe, it, expect } from 'vitest';
import { extractCapDenial, findCapState } from './capDenial';

// Shape emitted by ManagedLlmBudgetGate.cap_state (billing/managed_llm_budget.py)
// for a real denial.
const CAP_STATE = {
  plan: 'free',
  period: 'weekly',
  spent_cents: 33,
  limit_cents: 33,
  extra_usage_cents: 0,
  resets_at: '2026-08-01T00:00:00+00:00',
  retry_after_seconds: 3600,
  rate_limit_headers: { 'Retry-After': '3600' },
  purchase_url: '/billing/extra-usage/checkout',
  byok_setup_url: '/account/byok',
};

describe('extractCapDenial', () => {
  it('reads the cloud /v1/command envelope ({ok, error, data})', () => {
    const denial = extractCapDenial({ ok: true, error: null, data: { message: 'capped', cap_state: CAP_STATE } });
    expect(denial).toEqual({ plan: 'free', period: 'weekly', resetsAt: '2026-08-01T00:00:00+00:00' });
  });

  it('reads a doubly-nested data envelope', () => {
    const denial = extractCapDenial({ data: { data: { cap_state: CAP_STATE } } });
    expect(denial?.period).toBe('weekly');
  });

  it('reads a top-level cap_state (cloud dispatch payload)', () => {
    expect(extractCapDenial({ cap_state: CAP_STATE })?.plan).toBe('free');
  });

  // The single most important negative: ManagedLlmBudgetGate.cap_state returns
  // {} whenever the gate ALLOWED the turn, and a great many call sites pass
  // cap_state={} on success. Treating that as a denial would strand an upgrade
  // prompt on every normal answer.
  it('does not treat an empty cap_state as a denial', () => {
    expect(extractCapDenial({ data: { message: 'Playing music.', cap_state: {} } })).toBeNull();
    expect(findCapState({ data: { cap_state: {} } })).toBeNull();
  });

  it('returns null for an ordinary successful turn', () => {
    expect(extractCapDenial({ ok: true, data: { message: 'Playing music.', intent: 'music.play' } })).toBeNull();
  });

  it('returns null for junk input', () => {
    expect(extractCapDenial(null)).toBeNull();
    expect(extractCapDenial(undefined)).toBeNull();
    expect(extractCapDenial('nope')).toBeNull();
    expect(extractCapDenial([])).toBeNull();
    expect(extractCapDenial({ data: { cap_state: 'not-an-object' } })).toBeNull();
  });

  // services/cloud_intent/dispatch.py builds a FALLBACK gate with no period
  // when the reservation carries no gate, and that gate's cap_state serialises
  // to {}. Keying on cap_state alone would silently drop the affordance for
  // exactly that path, so the structural intent/source markers back it up.
  it('recognises the dispatch fallback denial that carries an empty cap_state', () => {
    const denial = extractCapDenial({
      ok: false,
      message: 'capped',
      source: 'managed_llm_spend_cap',
      intent: 'billing.cap_reached',
      cap_state: {},
    });
    expect(denial).toEqual({ plan: '', period: '', resetsAt: '' });
  });

  it('recognises the managed LLM forward denial (services/cloud_llm/routes.py)', () => {
    const denial = extractCapDenial({ ok: false, error: 'managed_llm_spend_cap', message: 'capped' });
    expect(denial).not.toBeNull();
  });

  it('does not fire on an unrelated error or intent', () => {
    expect(extractCapDenial({ ok: false, error: 'cost_circuit_breaker', message: 'busy' })).toBeNull();
    expect(extractCapDenial({ ok: true, data: { intent: 'billing.usage' } })).toBeNull();
  });

  // The denial copy is the one thing this must NOT key off: matching the
  // model's own words is the runtime-crutch anti-pattern .claude/rules/
  // agent-runtime.md forbids, and it breaks on any rewording or translation.
  it('ignores cap-sounding reply text with no structured marker', () => {
    expect(extractCapDenial({
      ok: true,
      data: { message: "You've reached your weekly managed AI limit. Upgrade or top up to continue." },
    })).toBeNull();
  });
});
