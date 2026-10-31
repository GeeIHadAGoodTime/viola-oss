import { describe, it, expect } from 'vitest';
import { appendAssistantEntry, mergeRestoredHistory } from './chatHistory';

describe('appendAssistantEntry', () => {
  it('appends a reply after a user turn', () => {
    const prev = [{ role: 'user', content: 'what time is it' }];
    const next = appendAssistantEntry(prev, "It's 7:05 PM.", 't1');
    expect(next).toEqual([...prev, { role: 'assistant', content: "It's 7:05 PM.", timestamp: 't1' }]);
  });

  it('drops the second copy of the same reply for the same turn', () => {
    const prev = [{ role: 'user', content: 'flip a coin' }];
    const once = appendAssistantEntry(prev, 'Heads.');
    const twice = appendAssistantEntry(once, 'Heads.');
    expect(twice).toBe(once);
    expect(twice.filter(m => m.role === 'assistant')).toHaveLength(1);
  });

  it('keeps an identical answer to a new question', () => {
    let h = [{ role: 'user', content: 'flip a coin' }];
    h = appendAssistantEntry(h, 'Heads.');
    h = [...h, { role: 'user', content: 'again' }];
    h = appendAssistantEntry(h, 'Heads.');
    expect(h.filter(m => m.role === 'assistant')).toHaveLength(2);
  });

  it('appends a different reply', () => {
    const prev = [{ role: 'assistant', content: 'One.' }];
    expect(appendAssistantEntry(prev, 'Two.')).toHaveLength(2);
  });
});

describe('mergeRestoredHistory (1.0.5 build 7: Chat History kept across a restart)', () => {
  const restored = [
    { role: 'user', content: 'play some jazz', timestamp: '2026-10-30T09:27:30Z' },
    { role: 'assistant', content: 'Playing relaxing jazz.', timestamp: '2026-10-30T09:27:45Z' },
  ];

  it('puts the saved turns before what this launch added', () => {
    const live = [{ role: 'user', content: 'flip a coin', timestamp: '2026-10-30T11:16:00Z' }];
    const merged = mergeRestoredHistory(live, restored);
    expect(merged.map((entry) => entry.content)).toEqual(['play some jazz', 'Playing relaxing jazz.', 'flip a coin']);
    expect(merged[0].restored).toBe(true);
    expect(merged[2].restored).toBeUndefined();
  });

  it('does not list a turn twice when this launch already has it', () => {
    const live = [{ role: 'assistant', content: 'Playing relaxing jazz.', timestamp: '2026-10-30T09:27:46Z' }];
    const merged = mergeRestoredHistory(live, restored);
    expect(merged.filter((entry) => entry.content === 'Playing relaxing jazz.')).toHaveLength(1);
  });

  it('keeps cleared turns cleared', () => {
    expect(mergeRestoredHistory([], restored, '2026-10-30T09:27:40Z').map((entry) => entry.content))
      .toEqual(['Playing relaxing jazz.']);
  });

  it('ignores a malformed response', () => {
    const live = [{ role: 'user', content: 'hi', timestamp: '2026-10-30T11:00:00Z' }];
    expect(mergeRestoredHistory(live, null)).toBe(live);
    expect(mergeRestoredHistory(live, [{ role: 'tool', content: 'x' }, { role: 'user', content: '' }])).toEqual(live);
  });

  it('replaces earlier restored entries instead of stacking them', () => {
    const once = mergeRestoredHistory([], restored);
    expect(mergeRestoredHistory(once, restored)).toHaveLength(2);
  });
});
