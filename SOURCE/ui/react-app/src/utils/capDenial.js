/**
 * capDenial — recognise a managed-AI usage-cap denial in a command result.
 *
 * When a managed-AI user runs out of their plan allowance, the backend answers
 * the turn with cap copy ("You've reached your ... limit ...") instead of a real
 * answer. That text used to be the whole story: it named upgrading but the user
 * had nothing to tap, which is friction at the exact moment they are trying to
 * pay us (candidate C-077).
 *
 * This module is the client-side half of the fix. It reads only STRUCTURED
 * fields the backend already emits and never inspects the reply text — matching
 * on the model's own words is the runtime-crutch anti-pattern that
 * `.claude/rules/agent-runtime.md` forbids, and it would break the moment the
 * copy is reworded or translated.
 *
 * The structural markers, and where they come from:
 *
 *   - `cap_state`  — `ManagedLlmBudgetGate.cap_state` (billing/managed_llm_budget.py).
 *                    Carries plan/period/reset. NOTE it is `{}` whenever the gate
 *                    ALLOWED the turn, so an empty object must never count as a
 *                    denial.
 *   - `intent: "billing.cap_reached"` and `source: "managed_llm_spend_cap"`
 *                  — services/cloud_intent/dispatch.py.
 *   - `error: "managed_llm_spend_cap"`
 *                  — services/cloud_llm/routes.py.
 *
 * The last three matter because one backend path builds a fallback gate with no
 * period, whose `cap_state` serialises to `{}`. Keying on `cap_state` alone
 * would silently drop the affordance for exactly that path.
 */

const DENIAL_INTENTS = new Set(['billing.cap_reached']);
const DENIAL_SOURCES = new Set(['managed_llm_spend_cap']);
const DENIAL_ERRORS = new Set(['managed_llm_spend_cap']);

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Envelope layers a command result may arrive wrapped in, outermost first. */
function candidateLayers(payload) {
  const layers = [];
  if (!isObject(payload)) return layers;
  layers.push(payload);
  if (isObject(payload.data)) {
    layers.push(payload.data);
    if (isObject(payload.data.data)) layers.push(payload.data.data);
  }
  return layers;
}

function readString(layer, key) {
  const value = layer[key];
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * Find the cap_state object on any envelope layer, ignoring the empty `{}` the
 * gate emits for an allowed turn.
 * @param {object} payload
 * @returns {object|null}
 */
export function findCapState(payload) {
  for (const layer of candidateLayers(payload)) {
    const state = layer.cap_state;
    if (isObject(state) && Object.keys(state).length > 0) return state;
  }
  return null;
}

/**
 * True when any layer carries a structural cap-denial marker other than
 * cap_state itself.
 * @param {object} payload
 * @returns {boolean}
 */
function hasDenialMarker(payload) {
  for (const layer of candidateLayers(payload)) {
    if (DENIAL_INTENTS.has(readString(layer, 'intent'))) return true;
    if (DENIAL_SOURCES.has(readString(layer, 'source'))) return true;
    if (DENIAL_ERRORS.has(readString(layer, 'error'))) return true;
  }
  return false;
}

/**
 * Normalise a command result into the state the cap notice renders from.
 *
 * @param {object} payload - a /v1/command result, voice-WS command_result, or
 *   cloud dispatch payload, in any of its envelope shapes.
 * @returns {{plan: string, period: string, resetsAt: string}|null} null when the
 *   turn was not denied by the managed-AI cap.
 */
export function extractCapDenial(payload) {
  const capState = findCapState(payload);
  if (!capState && !hasDenialMarker(payload)) return null;
  const state = capState || {};
  return {
    plan: typeof state.plan === 'string' ? state.plan.trim() : '',
    period: typeof state.period === 'string' ? state.period.trim() : '',
    resetsAt: typeof state.resets_at === 'string' ? state.resets_at.trim() : '',
  };
}

export default extractCapDenial;
