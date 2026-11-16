// Helpers for the tasks shown on the stage's quiet line (components/TaskLine.jsx).

const TOOL_ACTION_PREFIX = 'viola-tool-action:v1:';

/** A scheduled task (reminder, timer) rather than an agent the user started. */
export function isScheduledAgent(agent) {
  return agent?.subagent_type === 'scheduler' || String(agent?.reason || '').startsWith('scheduler:');
}

/**
 * What the task line calls a task: its short name when it has one (a
 * scheduled command's label, a background agent's name), otherwise its title.
 */
export function agentTaskName(agent) {
  const name = typeof agent?.name === 'string' ? agent.name.replace(/\s+/g, ' ').trim() : '';
  return name || agentTaskTitle(agent);
}

/**
 * The readable title of a task. Scheduled work is stored as an encoded tool
 * action; the backend now sends a readable title, and this keeps an older or
 * unexpected encoded value from ever reaching the screen raw (1.0.5 build 5
 * reminder card).
 */
export function agentTaskTitle(agent) {
  const raw = agent?.task || agent?.reason || 'Background task';
  if (typeof raw !== 'string' || !raw.startsWith(TOOL_ACTION_PREFIX)) return raw;
  try {
    const payload = JSON.parse(raw.slice(TOOL_ACTION_PREFIX.length));
    const args = payload && typeof payload.args === 'object' && payload.args ? payload.args : {};
    const text = (value) => (typeof value === 'string' ? value.trim() : '');
    if (payload?.tool === 'notify') {
      const message = text(args.message);
      if (!message) return 'Reminder';
      return /^reminder/i.test(message) ? message : `Reminder: ${message}`;
    }
    if (payload?.tool === 'alarm') return text(args.label) && text(args.label).toLowerCase() !== 'alarm' ? `Alarm: ${text(args.label)}` : 'Alarm';
    if (payload?.tool === 'playback') return args.reason === 'sleep_timer' ? 'Sleep timer' : 'Music';
  } catch { /* fall through to the generic title */ }
  return 'Scheduled task';
}
