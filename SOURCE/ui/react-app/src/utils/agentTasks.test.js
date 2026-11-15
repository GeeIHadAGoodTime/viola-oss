import { describe, expect, it } from 'vitest';
import { agentTaskTitle, isScheduledAgent } from './agentTasks';

describe('agentTaskTitle', () => {
  const encoded = 'viola-tool-action:v1:{"args":{"channel":null,"message":"Stretch"},"tool":"notify"}';

  it('never shows an encoded scheduler action', () => {
    expect(agentTaskTitle({ task: encoded, reason: 'scheduler:9' })).toBe('Reminder: Stretch');
  });

  it('labels alarms and sleep timers', () => {
    expect(agentTaskTitle({ task: 'viola-tool-action:v1:{"args":{"action":"sound","label":"Wake up"},"tool":"alarm"}' })).toBe('Alarm: Wake up');
    expect(agentTaskTitle({ task: 'viola-tool-action:v1:{"args":{"action":"stop","reason":"sleep_timer"},"tool":"playback"}' })).toBe('Sleep timer');
    expect(agentTaskTitle({ task: 'viola-tool-action:v1:not json' })).toBe('Scheduled task');
    expect(agentTaskTitle({ task: 'Book a table' })).toBe('Book a table');
    expect(agentTaskTitle({})).toBe('Background task');
  });
});

describe('isScheduledAgent', () => {
  it('matches the scheduler type or a scheduler reason only', () => {
    expect(isScheduledAgent({ subagent_type: 'scheduler' })).toBe(true);
    expect(isScheduledAgent({ reason: 'scheduler:42' })).toBe(true);
    expect(isScheduledAgent({ subagent_type: 'research', reason: 'user asked' })).toBe(false);
    expect(isScheduledAgent(null)).toBe(false);
  });
});
