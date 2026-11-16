/* eslint react/jsx-uses-vars: "error" */
// The quiet task line, and what it replaced: the bottom drawer of agent cards.
// 1.0.5 build 9: a card floated over the reply text, so the line keeps the same
// --agent-drawer-space contract (with a much smaller height).
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent } from '@testing-library/react';
import { render, screen } from '../test/test-utils';
import TaskLine from './TaskLine';

const space = () => document.documentElement.style.getPropertyValue('--agent-drawer-space');
const task = (id, name, extra = {}) => ({ agent_id: id, status: 'running', subagent_type: 'scheduler', task: name, ...extra });
const bg = (id, name, extra = {}) => ({ agent_id: id, status: 'running', subagent_type: 'research', task: name, ...extra });
const LINE = 'task-line';

function setup(active, extra = {}) {
  const onCancel = vi.fn();
  const props = { recentCompleted: [], streamStates: {}, onCancel, ...extra };
  const view = render(<TaskLine active={active} {...props} />);
  const update = (next, more = {}) => view.rerender(<TaskLine active={next} {...props} {...more} />);
  return { onCancel, update, ...view };
}
const advance = (ms) => act(() => { vi.advanceTimersByTime(ms); });

describe('TaskLine', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('shows one scheduled task as a line with its name and Stop, after the delay', () => {
    setup([task('a', 'Brindle soup notification')]);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
    advance(1300);
    const line = screen.getByTestId(LINE);
    expect(line).toHaveAttribute('role', 'status');
    expect(line).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByText('Scheduled')).toBeInTheDocument();
    expect(screen.queryByText('In the background')).not.toBeInTheDocument();
    expect(screen.getByText('Brindle soup notification')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop Brindle soup notification' })).toHaveTextContent('Stop');
    expect(screen.queryByTestId('agent-card')).not.toBeInTheDocument();
    expect(line.textContent).not.toMatch(/\d\d:\d\d|running/i);
  });

  it('shows a background agent under "In the background" with its own Stop', () => {
    const { onCancel } = setup([bg('b', 'Research electricians')]);
    advance(1300);
    expect(screen.getByText('In the background')).toBeInTheDocument();
    expect(screen.queryByText('Scheduled')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Stop Research electricians' }));
    expect(onCancel).toHaveBeenCalledWith('b');
    expect(screen.queryByTestId('agent-card')).not.toBeInTheDocument();
  });

  it('puts the current step of an agent in its tooltip, not on the line', () => {
    setup([bg('b', 'Research electricians')], { streamStates: { b: { activityText: 'reading a page' } } });
    advance(1300);
    expect(screen.getByTestId(LINE).textContent).not.toMatch(/reading a page/);
    expect(screen.getByText('Research electricians')).toHaveAttribute('title', 'Research electricians\nreading a page');
  });

  it('shows both groups, scheduled first, however the rows arrive', () => {
    setup([bg('b', 'Research electricians'), task('a', 'Stretch reminder')]);
    advance(1300);
    const groups = screen.getByTestId(LINE).querySelectorAll('[data-group]');
    expect([...groups].map((g) => g.getAttribute('data-group'))).toEqual(['scheduled', 'background']);
    const text = screen.getByTestId(LINE).textContent;
    expect(text.indexOf('Scheduled')).toBeLessThan(text.indexOf('Stretch reminder'));
    expect(text.indexOf('Stretch reminder')).toBeLessThan(text.indexOf('In the background'));
    expect(text.indexOf('In the background')).toBeLessThan(text.indexOf('Research electricians'));
  });

  it('Stop cancels that task by id', () => {
    const { onCancel } = setup([task('a', 'One'), task('b', 'Two')]);
    advance(1300);
    fireEvent.click(screen.getByRole('button', { name: 'Stop Two' }));
    expect(onCancel).toHaveBeenCalledWith('b');
  });

  it('puts three tasks side by side in order, with one "Scheduled"', () => {
    setup([task('a', 'First'), task('b', 'Second'), task('c', 'Third')]);
    advance(1300);
    expect(screen.getAllByText('Scheduled')).toHaveLength(1);
    const text = screen.getByTestId(LINE).textContent;
    expect(text.indexOf('First')).toBeLessThan(text.indexOf('Second'));
    expect(text.indexOf('Second')).toBeLessThan(text.indexOf('Third'));
    expect(screen.getAllByRole('button')).toHaveLength(3);
    expect(screen.queryByText(/more/)).not.toBeInTheDocument();
  });

  it('counts three items across the whole line and shows "+2 more" with the rest in the tooltip', () => {
    setup([
      task('s1', 'Sched 1'), task('s2', 'Sched 2'), task('s3', 'Sched 3'),
      bg('b1', 'Agent 1'), bg('b2', 'Agent 2'),
    ]);
    advance(1300);
    expect(screen.getAllByRole('button')).toHaveLength(3);
    expect(screen.queryByText('In the background')).not.toBeInTheDocument();
    expect(screen.getByText('+2 more').getAttribute('title')).toBe('Agent 1\nAgent 2');
  });

  it('never renders the line for a task that ends within 1.2 seconds', () => {
    const { update } = setup([task('a', 'Quick reminder')]);
    advance(600);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
    update([], { recentCompleted: [task('a', 'Quick reminder', { status: 'completed' })] });
    advance(2000);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
    expect(space()).toBe('');
  });

  it('applies the same delay to a background agent', () => {
    setup([bg('b', 'Research')]);
    advance(1000);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
    advance(300);
    expect(screen.getByTestId(LINE)).toBeInTheDocument();
  });

  it('fades out when a task completes and leaves nothing behind', () => {
    const { update } = setup([bg('b', 'Research')]);
    advance(1300);
    update([], { recentCompleted: [bg('b', 'Research', { status: 'completed' })] });
    expect(screen.queryByText(/completed|done/i)).not.toBeInTheDocument();
    advance(300);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
  });

  it("says a task didn't finish for about 4 seconds after a failure, then goes", () => {
    const { update } = setup([bg('b', 'Water the plants')]);
    advance(1300);
    update([], { recentCompleted: [bg('b', 'Water the plants', { status: 'failed' })] });
    expect(screen.getByText("Water the plants didn't finish")).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Stop/ })).not.toBeInTheDocument();
    advance(3500);
    expect(screen.getByText("Water the plants didn't finish")).toBeInTheDocument();
    advance(1000);
    advance(300);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
  });

  it('removes a cancelled task without a message', () => {
    const { update } = setup([task('a', 'Reminder')]);
    advance(1300);
    update([], { recentCompleted: [task('a', 'Reminder', { status: 'cancelled' })] });
    expect(screen.queryByText(/didn't finish/)).not.toBeInTheDocument();
    advance(300);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
  });

  it('ignores old failures from before the page looked', () => {
    setup([], { recentCompleted: [task('old', 'Stale', { status: 'failed', completed_at: '2020-01-01T00:00:00Z' })] });
    advance(2000);
    expect(screen.queryByTestId(LINE)).not.toBeInTheDocument();
  });

  it('recognises a scheduler reason as well as the scheduler type', () => {
    setup([{ agent_id: 'r', status: 'running', reason: 'scheduler:abc', task: 'By reason' }]);
    advance(1300);
    expect(screen.getByText('Scheduled')).toBeInTheDocument();
  });

  it('keeps room for the line while it shows and gives it back after', () => {
    const { update, unmount } = setup([task('a', 'Reminder')]);
    expect(space()).toBe('');
    advance(1300);
    expect(space()).toMatch(/^\d+px$/);
    update([]);
    advance(300);
    expect(space()).toBe('');
    update([bg('b', 'Again')]);
    advance(1300);
    expect(space()).toMatch(/^\d+px$/);
    unmount();
    expect(space()).toBe('');
  });

  it('cuts a long name to 48 characters and keeps the full text in the tooltip', () => {
    const long = 'Remind me to call the electrician about the broken porch light on Friday morning';
    setup([task('a', long)]);
    advance(1300);
    const name = screen.getByTitle(long);
    expect(name.textContent.length).toBe(48);
    expect(name.textContent.endsWith('\u2026')).toBe(true);
  });

  // Live (livecheck4): the line read "Search the web for the height of the tallest li\u2026"
  // for a scheduled command and the agent's own long prompt for a background agent.
  it('shows the short name of a scheduled task and keeps the full task text in the tooltip', () => {
    const long = 'Search the web for the height of the tallest lighthouse in Velmora and say it';
    setup([task('a', long, { name: 'Lighthouse height' })]);
    advance(1300);
    const name = screen.getByText('Lighthouse height');
    expect(name).toHaveAttribute('title', long);
    expect(screen.getByRole('button', { name: 'Stop Lighthouse height' })).toBeInTheDocument();
    expect(screen.getByTestId(LINE).textContent).not.toContain('Search the web');
  });

  it('shows the short name of a background agent; the tooltip has the full text and the step', () => {
    const long = 'Write an essay of exactly 300 words comparing the quorrel harp and the lute';
    setup([bg('b', long, { name: 'Harp essay' })], { streamStates: { b: { activityText: 'writing' } } });
    advance(1300);
    expect(screen.getByText('Harp essay')).toHaveAttribute('title', `${long}\nwriting`);
  });

  it('falls back to the title when the name is missing or blank, and cuts a long name at 48', () => {
    const longName = 'Look up the opening hours of every Tamberlin bakery in the old town';
    setup([task('a', 'Brindle soup notification', { name: '   ' }), bg('b', 'Research electricians', { name: null }), task('c', 'Short task', { name: longName })]);
    advance(1300);
    expect(screen.getByText('Brindle soup notification')).toBeInTheDocument();
    expect(screen.getByText('Research electricians')).toBeInTheDocument();
    const cut = screen.getByTitle('Short task');
    expect(cut.textContent.length).toBe(48);
    expect(cut.textContent.endsWith('\u2026')).toBe(true);
    expect(longName.startsWith(cut.textContent.slice(0, -1))).toBe(true);
  });

  it('names a failed task by its short name', () => {
    const { update } = setup([task('a', 'Search the web for the depth of Lake Orrin', { name: 'Lake depth' })]);
    advance(1300);
    update([], { recentCompleted: [task('a', 'Search the web for the depth of Lake Orrin', { name: 'Lake depth', status: 'failed' })] });
    expect(screen.getByText("Lake depth didn't finish")).toHaveAttribute('title', 'Search the web for the depth of Lake Orrin');
  });

  it('shows a readable title for an encoded tool action', () => {
    const raw = `viola-tool-action:v1:${JSON.stringify({ tool: 'notify', args: { message: 'stretch' } })}`;
    setup([task('a', raw)]);
    advance(1300);
    expect(screen.getByText('Reminder: stretch')).toBeInTheDocument();
  });

  it('never renders an agent card in any state', () => {
    const { update } = setup([task('a', 'One'), bg('b', 'Two')]);
    advance(1300);
    expect(screen.queryByTestId('agent-card')).not.toBeInTheDocument();
    update([], { recentCompleted: [task('a', 'One', { status: 'failed' }), bg('b', 'Two', { status: 'completed' })] });
    expect(screen.queryByTestId('agent-card')).not.toBeInTheDocument();
  });
});

describe('the stage makes room for the line', () => {
  it('keeps the bottom row out from under it', () => {
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

  it('has no animation under reduced motion', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/components/TaskLine.module.css'), 'utf8');
    const reduced = css.slice(css.indexOf('prefers-reduced-motion'));
    expect(reduced).toMatch(/\.working/);
    expect(reduced).toContain('animation: none;');
  });
});

describe('the top bar badge is gone', () => {
  it('has no agent count or drawer toggle in the top bar source', () => {
    const jsx = readFileSync(resolve(process.cwd(), 'src/components/topbar/TopBar.jsx'), 'utf8');
    expect(jsx).not.toMatch(/activeAgentCount|onToggleAgentDrawer|agentCountButton/);
  });
});
