import { describe, it, expect } from 'vitest';
import { appendAssistantEntry } from './chatHistory';

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
