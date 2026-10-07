import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('../hooks/useViolaApi', () => ({ apiFetch }));

let HistoryModal;
let applyTheme;
let THEME;

const thread = { id: 'theme-thread', title: 'A saved conversation' };
const messages = [
  { id: 'user', role: 'user', content: 'A saved question' },
  { id: 'assistant', role: 'assistant', content: 'A saved answer' },
];
const modal = () => <HistoryModal isOpen onClose={() => {}} principalKey="theme-owner" />;
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
};
const normalizedColor = (color) => {
  const element = document.createElement('span');
  element.style.color = color;
  return element.style.color;
};
const expectSecondaryButton = (element) => {
  const style = getComputedStyle(element);
  expect(style.color).toBe(normalizedColor(THEME.colors.textSecondary));
  expect(style.backgroundColor).toBe(normalizedColor(THEME.colors.glassBase));
};
const expectPrimary = (element) => {
  // Inspect the rendered descendant, not just the section's style declaration.
  expect(getComputedStyle(element).color).toBe(normalizedColor(THEME.colors.textPrimary));
};

beforeEach(async () => {
  vi.resetModules();
  vi.clearAllMocks();
  apiFetch.mockReset();
  // Reproduce cached Light at module load, then an authoritative setting
  // switching the mounted History controls to another palette.
  localStorage.setItem('viola_theme_mode', 'light');
  ({ applyTheme, THEME } = await import('../config'));
  ({ default: HistoryModal } = await import('./HistoryModal'));
  // The app's themed inner card is a sibling of HistoryModal, so it cannot
  // supply an inherited foreground. Model the browser's ordinary black text.
  document.body.style.color = 'rgb(0, 0, 0)';
});
afterEach(() => {
  cleanup();
  document.body.style.color = '';
  applyTheme('dark');
});

describe.each([
  ['dark', true],
  ['light', false],
  ['system', true],
  ['system', false],
])('Saved history foreground in %s mode (system dark: %s)', (mode, systemDark) => {
  beforeEach(() => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: systemDark });
    applyTheme(mode);
  });

  it('keeps loading, failure, retry, and empty saved-list text themed', async () => {
    const initial = deferred();
    const retry = deferred();
    apiFetch.mockReturnValueOnce(initial.promise).mockReturnValueOnce(retry.promise);
    render(modal());
    expectSecondaryButton(screen.getByRole('button', { name: 'Close history modal' }));
    expectPrimary(screen.getByText('Loading saved chats...'));
    await act(async () => initial.reject(new Error('offline')));
    expectPrimary(screen.getByRole('alert'));
    expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
    expectSecondaryButton(screen.getByRole('button', { name: 'Retry saved chats' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry saved chats' }));
    expectPrimary(screen.getByText('Loading saved chats...'));
    await act(async () => retry.resolve({ threads: [] }));
    expectPrimary(screen.getByText('No saved chats yet.'));
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });

  it('keeps the selected heading, recovery states, and both message roles themed', async () => {
    const selected = deferred();
    const retry = deferred();
    apiFetch.mockResolvedValueOnce({ threads: [thread] })
      .mockReturnValueOnce(selected.promise).mockReturnValueOnce(retry.promise);
    render(modal());
    expectSecondaryButton(screen.getByRole('button', { name: 'Close history modal' }));
    const threadButton = await screen.findByRole('button', { name: thread.title });
    expectSecondaryButton(threadButton);
    fireEvent.click(threadButton);
    expectPrimary(screen.getByRole('heading', { name: thread.title }));
    expectPrimary(screen.getByText('Loading conversation...'));
    await act(async () => selected.reject(Object.assign(new Error('consent'), { code: 'consent_required' })));
    expectPrimary(screen.getByRole('alert'));
    expect(screen.getByRole('alert')).toHaveTextContent('Cloud Sync');
    expectSecondaryButton(screen.getByRole('button', { name: 'Retry conversation' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry conversation' }));
    expectPrimary(screen.getByText('Loading conversation...'));
    await act(async () => retry.resolve({ messages }));
    for (const message of messages) expectPrimary(screen.getByText(message.content));
    expect(screen.getByRole('button', { name: thread.title })).toHaveAttribute('aria-pressed', 'true');
    expect(getComputedStyle(screen.getByText('You')).color).toBe(normalizedColor(THEME.colors.textMuted));
    expect(getComputedStyle(screen.getByText('Viola')).color).toBe(normalizedColor(THEME.colors.textMuted));
    expect(apiFetch).toHaveBeenCalledTimes(3);
  });

  it('keeps an empty selected conversation themed', async () => {
    apiFetch.mockResolvedValueOnce({ threads: [thread] }).mockResolvedValueOnce({ messages: [] });
    render(modal());
    expectSecondaryButton(screen.getByRole('button', { name: 'Close history modal' }));
    const threadButton = await screen.findByRole('button', { name: thread.title });
    expectSecondaryButton(threadButton);
    fireEvent.click(threadButton);
    expectPrimary(await screen.findByText('No messages in this conversation yet.'));
  });
});

it('updates loaded history on a theme rerender without clearing or rereading it', async () => {
  applyTheme('dark');
  apiFetch.mockResolvedValueOnce({ threads: [thread] }).mockResolvedValueOnce({ messages });
  const view = render(modal());
  fireEvent.click(await screen.findByRole('button', { name: thread.title }));
  expectPrimary(await screen.findByText('A saved answer'));
  for (const mode of ['light', 'dark']) {
    applyTheme(mode);
    view.rerender(modal());
    expectPrimary(screen.getByRole('heading', { name: thread.title }));
    expectSecondaryButton(screen.getByRole('button', { name: thread.title }));
    expectSecondaryButton(screen.getByRole('button', { name: 'Close history modal' }));
    for (const message of messages) expectPrimary(screen.getByText(message.content));
    expect(screen.getByRole('button', { name: thread.title })).toHaveAttribute('aria-pressed', 'true');
  }
  expect(apiFetch).toHaveBeenCalledTimes(2);
});
