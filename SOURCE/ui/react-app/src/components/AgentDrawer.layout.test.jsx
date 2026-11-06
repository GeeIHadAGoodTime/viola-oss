// 1.0.5 build 9: "Running a scheduled task" floated over the reply text. The
// drawer is fixed to the bottom of the window, so the bottom row has to move up
// for it. It also pins the browser stage, whose fixed minimum height made a dark
// band spill over the reply text in a short window.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { render } from '../test/test-utils';
import AgentDrawer from './AgentDrawer';

const space = () => document.documentElement.style.getPropertyValue('--agent-drawer-space');
const agent = { agent_id: 'a1', status: 'running', subagent_type: 'scheduler', task: 'Reminder' };
const props = { recentCompleted: [], streamStates: {}, expanded: true, onToggleExpanded: vi.fn(), onCancel: vi.fn() };

describe('background agents and the reply text', () => {
  it('makes room while a card is up and gives it back after', () => {
    const { rerender, unmount } = render(<AgentDrawer {...props} active={[agent]} />);
    expect(space()).toMatch(/^\d+px$/);

    rerender(<AgentDrawer {...props} active={[]} />);
    expect(space()).toBe('');

    rerender(<AgentDrawer {...props} active={[agent]} />);
    expect(space()).toMatch(/^\d+px$/);
    unmount();
    expect(space()).toBe('');
  });

  it('asks nothing of the layout with no agents', () => {
    render(<AgentDrawer {...props} active={[]} />);
    expect(space()).toBe('');
  });

  it('keeps the bottom row out from under the drawer', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/components/voice/BottomRow.module.css'), 'utf8');
    expect(css).toContain('margin-bottom: var(--agent-drawer-space, 0px);');
  });

  it('lets the browser stage shrink with the stage', () => {
    const css = readFileSync(
      resolve(process.cwd(), 'src/components/stage/modes/browser/BrowserMode.module.css'),
      'utf8',
    );
    const container = css.slice(css.indexOf('.container'), css.indexOf('}', css.indexOf('.container')));
    expect(container).toContain('min-height: 0;');
  });
});
