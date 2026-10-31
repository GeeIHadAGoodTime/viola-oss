import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { matchesSearch } from './settingsSearch';

const here = dirname(fileURLToPath(import.meta.url));
const TABS = [
  { id: 'account', label: 'Account' },
  { id: 'ai_agents', label: 'AI & Agents' },
  { id: 'music', label: 'Music' },
  { id: 'voice', label: 'Voice' },
  { id: 'connections', label: 'Connections' },
  { id: 'customize', label: 'Customize' },
  { id: 'system', label: 'System' },
];
const tab = (id) => TABS.find((entry) => entry.id === id);
const found = (query) => TABS.filter((entry) => matchesSearch(entry, query)).map((entry) => entry.id);

// Titles of settings rows, sections and pickers, read from the source so a
// newly added setting is searchable from day one.
const TITLE_RE = /<(SettingRow|Section|Select|Slider|InfoRow|AdvancedSection)\b[^<]*?\b(?:title|label)="([^"]+)"/g;

function titlesByTab() {
  const modal = readFileSync(join(here, 'SettingsModal.jsx'), 'utf8');
  const lines = modal.split('\n');
  const starts = [];
  lines.forEach((line, index) => {
    const match = /^\s*case '(\w+)':/.exec(line);
    if (match && TABS.some((entry) => entry.id === match[1])) starts.push([match[1], index]);
  });
  const byTab = {};
  starts.forEach(([id, start], index) => {
    const end = index + 1 < starts.length ? starts[index + 1][1] : lines.length;
    const segment = lines.slice(start, end).join('\n');
    byTab[id] = [...segment.matchAll(TITLE_RE)].map((match) => match[2]);
  });
  const customize = readFileSync(join(here, 'CustomizeTab.jsx'), 'utf8');
  byTab.customize = [...customize.matchAll(TITLE_RE)].map((match) => match[2]);
  return byTab;
}

describe('Settings search (1.0.5 build 7, A2.8)', () => {
  it('finds Automatic Crash Reports by "crash"', () => {
    expect(found('crash')).toContain('account');
    expect(found('Crash Reports')).toContain('account');
    expect(found('reports crash')).toContain('account');
  });

  it('finds other settings by the name shown on screen', () => {
    expect(found('assistant voice')).toContain('voice');
    expect(found('tray')).toContain('system');
    expect(found('double-check')).toContain('voice');
    expect(found('speaking speed')).toContain('voice');
  });

  it('still says nothing matches for a word no setting uses', () => {
    expect(found('zebra')).toEqual([]);
  });

  it('finds every setting title shown in Settings under its own tab', () => {
    const byTab = titlesByTab();
    expect(Object.keys(byTab).sort()).toEqual(TABS.map((entry) => entry.id).sort());
    const missing = [];
    Object.entries(byTab).forEach(([id, titles]) => {
      titles.forEach((title) => {
        if (!matchesSearch(tab(id), title)) missing.push(`${id}: ${title}`);
      });
    });
    expect(missing).toEqual([]);
  });
});
