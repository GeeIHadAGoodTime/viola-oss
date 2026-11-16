/* eslint react/jsx-uses-vars: "error" */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { agentTaskName, agentTaskTitle, isScheduledAgent } from '../utils/agentTasks';
import styles from './TaskLine.module.css';

// Running tasks are one quiet line of text at the bottom left of the stage:
// scheduled tasks (reminders, timers) first, then agents the user started in
// the background. A task shows up only after it has run for a moment, so one
// that starts and ends at once never flashes.
export const SHOW_AFTER_MS = 1200;
export const FAILED_VISIBLE_MS = 4000;
export const FADE_MS = 200;
export const MAX_ITEMS = 3;
export const MAX_NAME_CHARS = 48;
const LINE_GAP_PX = 14;

// Same horizontal padding as the stage card, so the line starts at the clock's edge.
export const STAGE_SIDE_PADDING = 'clamp(36px, 5.5vw, 72px)';

const TERMINAL = ['completed', 'cancelled', 'killed', 'error', 'failed'];
const FAILED = ['error', 'failed'];

// The short name when the task has one, else its title; the tooltip keeps the full task text.
export function shortTaskName(agent) {
  const text = String(agentTaskName(agent)).replace(/\s+/g, ' ').trim();
  return text.length > MAX_NAME_CHARS ? `${text.slice(0, MAX_NAME_CHARS - 1).trimEnd()}…` : text;
}

function statusOf(agent, streamStates) {
  return streamStates?.[agent.agent_id]?.status || agent.status || 'running';
}

function finishedAt(agent, streamStates) {
  const fromStream = streamStates?.[agent.agent_id]?.completedAt;
  if (fromStream) return fromStream;
  const parsed = Date.parse(agent.completed_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

// The full name, then the step the agent is on, for the hover text.
function tooltipFor(agent, streamStates) {
  const full = String(agentTaskTitle(agent));
  const step = String(streamStates?.[agent.agent_id]?.activityText || '').trim();
  return step && !isScheduledAgent(agent) ? `${full}\n${step}` : full;
}

function ClockIcon() {
  return (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  );
}

// "Working": a ring with a pulse line through it, in the same stroke style.
function WorkingIcon() {
  return (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M6.5 12h3l1.5-3.5 2 7 1.5-3.5h3" />
    </>
  );
}

const GROUPS = [
  { key: 'scheduled', label: 'Scheduled', Icon: ClockIcon, pulse: false },
  { key: 'background', label: 'In the background', Icon: WorkingIcon, pulse: true },
];

export default function TaskLine({ active = [], recentCompleted = [], streamStates = {}, onCancel }) {
  const [, setTick] = useState(0);
  const firstSeen = useRef(new Map());
  const everRunning = useRef(new Set());
  const failures = useRef(new Map());
  const lastItems = useRef([]);
  const [leaving, setLeaving] = useState(null);
  const lineRef = useRef(null);

  const now = Date.now();
  const runningIds = new Set();
  const running = [];
  active.forEach((agent) => {
    if (!agent.agent_id || runningIds.has(agent.agent_id)) return;
    if (TERMINAL.includes(statusOf(agent, streamStates))) return;
    runningIds.add(agent.agent_id);
    running.push(agent);
  });
  // Bookkeeping that must outlive a render, written before anything reads it.
  running.forEach((agent) => {
    if (!firstSeen.current.has(agent.agent_id)) firstSeen.current.set(agent.agent_id, now);
    everRunning.current.add(agent.agent_id);
  });
  [...firstSeen.current.keys()].forEach((id) => { if (!runningIds.has(id)) firstSeen.current.delete(id); });

  [...active, ...recentCompleted].forEach((agent) => {
    if (!agent.agent_id || failures.current.has(agent.agent_id)) return;
    if (!FAILED.includes(statusOf(agent, streamStates))) return;
    const done = finishedAt(agent, streamStates);
    // A failure from before this page looked is old news, not something to show.
    if (!everRunning.current.has(agent.agent_id) && !(done && now - done < FAILED_VISIBLE_MS)) return;
    failures.current.set(agent.agent_id, {
      name: shortTaskName(agent),
      full: String(agentTaskTitle(agent)),
      scheduled: isScheduledAgent(agent),
      until: now + FAILED_VISIBLE_MS,
    });
  });

  const built = [];
  running.forEach((agent) => {
    if (now - firstSeen.current.get(agent.agent_id) < SHOW_AFTER_MS) return;
    built.push({
      id: agent.agent_id,
      name: shortTaskName(agent),
      tip: tooltipFor(agent, streamStates),
      scheduled: isScheduledAgent(agent),
      failed: false,
    });
  });
  failures.current.forEach((failure, id) => {
    if (failure.until > now && !runningIds.has(id)) {
      built.push({ id, name: failure.name, tip: failure.full, scheduled: failure.scheduled, failed: true });
    }
  });
  // Scheduled first, then background; each keeps its own order.
  const items = [...built.filter((i) => i.scheduled), ...built.filter((i) => !i.scheduled)];

  // Wake up at the next moment something has to appear or disappear.
  const deadlines = [];
  running.forEach((agent) => {
    const due = firstSeen.current.get(agent.agent_id) + SHOW_AFTER_MS;
    if (due > now) deadlines.push(due);
  });
  failures.current.forEach((failure) => { if (failure.until > now) deadlines.push(failure.until); });
  const nextDeadline = deadlines.length ? Math.min(...deadlines) : 0;
  useEffect(() => {
    if (!nextDeadline) return undefined;
    const timerId = window.setTimeout(() => setTick((n) => n + 1), Math.max(0, nextDeadline - Date.now()) + 5);
    return () => window.clearTimeout(timerId);
  }, [nextDeadline]);

  // When the last task leaves, the line fades out instead of vanishing.
  const has = items.length > 0;
  if (has) lastItems.current = items;
  useEffect(() => {
    if (has) {
      setLeaving(null);
      return undefined;
    }
    if (lastItems.current.length === 0) return undefined;
    setLeaving(lastItems.current);
    lastItems.current = [];
    const timerId = window.setTimeout(() => setLeaving(null), FADE_MS);
    return () => window.clearTimeout(timerId);
  }, [has]);

  const shown = has ? items : (leaving || []);
  const showing = shown.length > 0;

  // The line sits over the bottom edge of the stage; this tells the bottom row
  // how much room to keep. Same contract the old background-agent drawer had
  // (1.0.5 build 9: a card sat on top of the reply text), with a much smaller height.
  useLayoutEffect(() => {
    const node = lineRef.current;
    if (!showing || !node) return undefined;
    const root = document.documentElement;
    const publish = () => {
      root.style.setProperty('--agent-drawer-space', `${Math.ceil(node.getBoundingClientRect().height) + LINE_GAP_PX}px`);
    };
    publish();
    const observer = typeof window.ResizeObserver === 'function' ? new window.ResizeObserver(publish) : null;
    observer?.observe(node);
    return () => {
      observer?.disconnect();
      root.style.removeProperty('--agent-drawer-space');
    };
  }, [showing]);

  if (!showing) return null;

  const visible = shown.slice(0, MAX_ITEMS);
  const rest = shown.slice(MAX_ITEMS);
  const muted = { color: THEME.colors.textMuted };
  const groups = GROUPS
    .map((group) => ({ ...group, items: visible.filter((item) => item.scheduled === (group.key === 'scheduled')) }))
    .filter((group) => group.items.length > 0);

  return (
    <div
      ref={lineRef}
      role="status"
      aria-live="polite"
      data-testid="task-line"
      className={`${styles.line}${has ? '' : ` ${styles.leaving}`}`}
      style={{
        left: STAGE_SIDE_PADDING,
        right: STAGE_SIDE_PADDING,
        bottom: '28px',
        fontFamily: THEME.fonts.sans,
        ...muted,
      }}
    >
      {groups.map((group, groupIndex) => (
        <span key={group.key} className={styles.group} data-group={group.key}>
          <span className={styles.lead}>
            <svg
              className={`${styles.icon}${group.pulse ? ` ${styles.working}` : ''}`}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <group.Icon />
            </svg>
            <span>{group.label}</span>
          </span>
          {group.items.map((item) => (
            <span key={item.id} className={styles.item}>
              <span className={styles.name} title={item.tip} style={{ color: THEME.colors.textSecondary }}>
                {item.failed ? `${item.name} didn't finish` : item.name}
              </span>
              {item.failed ? null : (
                <>
                  <span aria-hidden="true">{'·'}</span>
                  <button
                    type="button"
                    className={styles.stop}
                    aria-label={`Stop ${item.name}`}
                    style={muted}
                    onClick={() => { void Promise.resolve(onCancel(item.id)).catch(() => {}); }}
                  >
                    Stop
                  </button>
                </>
              )}
            </span>
          ))}
          {groupIndex === groups.length - 1 && rest.length > 0 ? (
            <span className={styles.more} title={rest.map((item) => item.tip.split('\n')[0]).join('\n')}>{`+${rest.length} more`}</span>
          ) : null}
        </span>
      ))}
    </div>
  );
}

TaskLine.propTypes = {
  active: PropTypes.arrayOf(PropTypes.object),
  recentCompleted: PropTypes.arrayOf(PropTypes.object),
  streamStates: PropTypes.object,
  onCancel: PropTypes.func.isRequired,
};
