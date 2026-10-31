// One assistant reply reaches the chat history from two places for the same
// turn: the backend broadcasts a `chat_response` WebSocket event BEFORE the
// HTTP command response returns (ui/api/routes/command.py), and the HTTP
// result is then handled by handleCommandResult. Whichever lands second must
// not add a second copy, so both paths append through this helper.
//
// A reply is treated as the same turn's echo only when it is identical to the
// LAST entry and that entry is an assistant entry. A new typed or spoken turn
// appends its user entry first, so an identical answer to a new question (two
// coin flips both "Heads.") is still recorded.
export function appendAssistantEntry(prev, content, timestamp = new Date().toISOString()) {
  const list = Array.isArray(prev) ? prev : [];
  const last = list.length > 0 ? list[list.length - 1] : null;
  if (last && last.role === 'assistant' && last.content === content) return list;
  return [...list, { role: 'assistant', content, timestamp }];
}

// Chat History used to list only the turns since the last launch: the list
// lived in memory and a restart emptied it (1.0.5 build-7 laptop report). The
// backend keeps every turn in the user's encrypted conversation log and
// serves the recent ones from /v1/chat/recent-activity; this puts them in
// front of what this launch has already added.
//
// A turn can be in both lists (it was saved before the read came back). One
// is dropped when role and text match an entry this launch already has within
// two minutes. Entries from before "Clear Recent Activity" (`clearedAt`, an
// ISO time) stay cleared.
const SAME_TURN_WINDOW_MS = 2 * 60 * 1000;

function entryTime(entry) {
  const ms = Date.parse(entry?.timestamp || '');
  return Number.isNaN(ms) ? null : ms;
}

export function mergeRestoredHistory(prev, restored, clearedAt = null) {
  const list = Array.isArray(prev) ? prev : [];
  if (!Array.isArray(restored) || restored.length === 0) return list;
  const clearedMs = clearedAt ? Date.parse(clearedAt) : NaN;
  const live = list.filter((entry) => !entry?.restored);
  const kept = restored.filter((entry) => {
    if (!entry || (entry.role !== 'user' && entry.role !== 'assistant')) return false;
    if (typeof entry.content !== 'string' || !entry.content.trim()) return false;
    const at = entryTime(entry);
    if (!Number.isNaN(clearedMs) && (at === null || at <= clearedMs)) return false;
    return !live.some((current) => {
      if (current?.role !== entry.role || current?.content !== entry.content) return false;
      const currentAt = entryTime(current);
      return at === null || currentAt === null || Math.abs(currentAt - at) <= SAME_TURN_WINDOW_MS;
    });
  }).map((entry) => ({ role: entry.role, content: entry.content, timestamp: entry.timestamp, restored: true }));
  return [...kept, ...live];
}

const CLEARED_AT_KEY_PREFIX = 'viola_recent_activity_cleared_at:';

export function readRecentActivityClearedAt(principalKey) {
  try {
    return window.localStorage.getItem(CLEARED_AT_KEY_PREFIX + principalKey) || null;
  } catch {
    return null;
  }
}

export function rememberRecentActivityCleared(principalKey, at = new Date().toISOString()) {
  try {
    window.localStorage.setItem(CLEARED_AT_KEY_PREFIX + principalKey, at);
  } catch {
    // Private window or blocked storage: the list is still cleared for now.
  }
}
