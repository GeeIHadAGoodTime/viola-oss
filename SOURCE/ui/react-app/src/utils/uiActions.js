const COMMAND_RESULTS_KEYS = ['command_results', 'commands_executed'];

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function commandResultEntries(commandResults) {
  if (Array.isArray(commandResults)) return commandResults;
  if (isObject(commandResults)) return Object.values(commandResults);
  return [];
}

function addAction(actions, action, payload) {
  if (typeof action !== 'string' || !action.trim()) return;
  const normalized = action.trim();
  const detailPayload = isObject(payload) ? payload : {};
  const key = JSON.stringify({
    action: normalized,
    tab: detailPayload.tab || detailPayload.rooms_modal_tab || '',
    room: detailPayload.room_name || detailPayload.target_room || '',
    panel: detailPayload.panel_id || detailPayload.path_identifier || '',
  });
  if (actions.some((entry) => entry.key === key)) return;
  actions.push({ key, action: normalized, payload: detailPayload });
}

function collectFromEnvelope(actions, envelope) {
  if (!isObject(envelope)) return;

  addAction(actions, envelope.ui_action, envelope);

  if (isObject(envelope.data)) {
    addAction(actions, envelope.data.ui_action, envelope.data);
    if (isObject(envelope.data.data)) {
      addAction(actions, envelope.data.data.ui_action, envelope.data.data);
    }
  }

  const pairingFlow = isObject(envelope.pairing_flow)
    ? envelope.pairing_flow
    : isObject(envelope.data?.pairing_flow)
      ? envelope.data.pairing_flow
      : null;
  if (pairingFlow) {
    const parentPayload = isObject(envelope.data) ? envelope.data : envelope;
    addAction(actions, pairingFlow.ui_action, { ...parentPayload, ...pairingFlow });
  }
}

function collectFromAiData(actions, aiData) {
  if (!isObject(aiData)) return;

  for (const key of COMMAND_RESULTS_KEYS) {
    for (const entry of commandResultEntries(aiData[key])) {
      collectFromEnvelope(actions, entry);
      if (isObject(entry?.data)) collectFromEnvelope(actions, entry.data);
    }
  }
}

export function collectUiActions(payload) {
  const actions = [];
  collectFromEnvelope(actions, payload);

  if (isObject(payload?.data)) {
    collectFromEnvelope(actions, payload.data);
    collectFromAiData(actions, payload.data.ai_data);
  }

  collectFromAiData(actions, payload?.ai_data);

  return actions.map(({ action, payload: actionPayload }) => ({ action, payload: actionPayload }));
}

export function dispatchUiActions(payload) {
  if (typeof window === 'undefined') return [];
  const actions = collectUiActions(payload);
  actions.forEach(({ action, payload: actionPayload }) => {
    window.dispatchEvent(new CustomEvent('viola:ui-action', {
      detail: { action, payload: actionPayload },
    }));
  });
  return actions;
}
