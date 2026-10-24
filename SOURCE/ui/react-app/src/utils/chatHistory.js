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
