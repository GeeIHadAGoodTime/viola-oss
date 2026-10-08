import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ChatMessage from './ChatMessage.jsx';

const initialMessage = { id: 'message-1', role: 'assistant', content: 'Hello **world**' };
const callbacks = { onRegenerate: vi.fn(), onFork: vi.fn(), onFeedback: vi.fn() };
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function installClipboard(clipboard) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: clipboard });
}

function renderMessage(message = initialMessage) {
  const element = (next) => <StrictMode><ChatMessage message={next} {...callbacks} /></StrictMode>;
  const result = render(element(message));
  return { ...result, update: (next) => result.rerender(element(next)) };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard);
  else delete navigator.clipboard;
});

const surfaces = [
  {
    name: 'message',
    message: initialMessage,
    copiedText: initialMessage.content,
    scope: (container) => within(container.querySelector('.chat-message-actions')),
  },
  {
    name: 'code block',
    message: { ...initialMessage, content: 'Example:\n```js\nconst greeting = "hello";\n```' },
    copiedText: 'const greeting = "hello";',
    scope: (container) => within(container.querySelector('.chat-code-bar')),
  },
];

describe.each(surfaces)('Chat $name clipboard feedback', ({ message, copiedText, scope }) => {
  it('starts the write in the click, waits for success, and then clears feedback', async () => {
    const write = deferred();
    const writeText = vi.fn(() => write.promise);
    installClipboard({ writeText });
    const { container } = renderMessage(message);
    const ui = scope(container);

    fireEvent.click(ui.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledExactlyOnceWith(copiedText);
    expect(ui.queryByRole('button', { name: 'Copied' })).not.toBeInTheDocument();
    const pending = ui.getByRole('button', { name: 'Copying…' });
    expect(pending).toBeDisabled();
    fireEvent.click(pending);
    expect(writeText).toHaveBeenCalledTimes(1);

    await act(async () => { write.resolve(); });
    const copied = ui.getByRole('button', { name: 'Copied' });
    expect(copied).toBeEnabled();
    expect(copied.querySelector('[aria-live="polite"]')).toHaveTextContent('Copied');
    act(() => { vi.advanceTimersByTime(1099); });
    expect(copied).toHaveTextContent('Copied');
    act(() => { vi.advanceTimersByTime(1); });
    expect(ui.getByRole('button', { name: 'Copy' })).toBeEnabled();
  });

  it('shows a rejected write as a retryable failure and recovers', async () => {
    const writeText = vi.fn().mockRejectedValueOnce(new Error('Denied')).mockResolvedValueOnce();
    installClipboard({ writeText });
    const { container } = renderMessage(message);
    const ui = scope(container);
    await act(async () => { fireEvent.click(ui.getByRole('button', { name: 'Copy' })); });
    const retry = ui.getByRole('button', { name: 'Copy failed. Retry' });
    expect(retry).toBeEnabled();
    expect(retry).toHaveAttribute('title', 'Try again, or select the text and copy it manually.');
    expect(ui.queryByRole('button', { name: 'Copied' })).not.toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(5000); });
    expect(retry).toHaveTextContent('Copy failed. Retry');
    await act(async () => { fireEvent.click(retry); });
    expect(writeText).toHaveBeenCalledTimes(2);
    expect(ui.getByRole('button', { name: 'Copied' })).toBeEnabled();
  });

  it.each([undefined, {}, { writeText: null }])('handles an unavailable clipboard API (%j)', async (clipboard) => {
    installClipboard(clipboard);
    const { container } = renderMessage(message);
    const ui = scope(container);
    await act(async () => { fireEvent.click(ui.getByRole('button', { name: 'Copy' })); });
    expect(ui.getByRole('button', { name: 'Copy failed. Retry' })).toBeEnabled();
    expect(ui.queryByRole('button', { name: 'Copied' })).not.toBeInTheDocument();
  });

  it('handles a synchronous write failure', async () => {
    installClipboard({ writeText: vi.fn(() => { throw new Error('Unavailable'); }) });
    const { container } = renderMessage(message);
    const ui = scope(container);
    await act(async () => { fireEvent.click(ui.getByRole('button', { name: 'Copy' })); });
    expect(ui.getByRole('button', { name: 'Copy failed. Retry' })).toBeEnabled();
  });

  it('gives repeated successful copies their own full feedback interval', async () => {
    installClipboard({ writeText: vi.fn().mockResolvedValue() });
    const { container } = renderMessage(message);
    const ui = scope(container);
    await act(async () => { fireEvent.click(ui.getByRole('button', { name: 'Copy' })); });
    act(() => { vi.advanceTimersByTime(1000); });
    await act(async () => { fireEvent.click(ui.getByRole('button', { name: 'Copied' })); });
    act(() => { vi.advanceTimersByTime(100); });
    expect(ui.getByRole('button', { name: 'Copied' })).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(ui.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it.each(['resolve', 'reject'])('ignores an old write that later %ss after the message changes', async (settle) => {
    const oldWrite = deferred();
    const newWrite = deferred();
    const writeText = vi.fn().mockReturnValueOnce(oldWrite.promise).mockReturnValueOnce(newWrite.promise);
    installClipboard({ writeText });
    const { container, update } = renderMessage(message);
    fireEvent.click(scope(container).getByRole('button', { name: 'Copy' }));
    update({ ...message, content: message.content.replace(/hello|Hello/, 'New') });
    fireEvent.click(scope(container).getByRole('button', { name: 'Copy' }));
    await act(async () => { oldWrite[settle](new Error('Old failure')); });
    expect(scope(container).getByRole('button', { name: 'Copying…' })).toBeDisabled();
    await act(async () => { newWrite.resolve(); });
    expect(scope(container).getByRole('button', { name: 'Copied' })).toBeInTheDocument();
    expect(writeText.mock.calls[1][0]).toBe(copiedText.replace(/hello|Hello/, 'New'));
  });

  it('resets feedback for a different message with identical text', async () => {
    installClipboard({ writeText: vi.fn().mockResolvedValue() });
    const { container, update } = renderMessage(message);
    await act(async () => { fireEvent.click(scope(container).getByRole('button', { name: 'Copy' })); });
    update({ ...message, id: 'message-2' });
    expect(scope(container).getByRole('button', { name: 'Copy' })).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(['resolve', 'reject'])('retires pending writes on unmount before they %s', async (settle) => {
    const write = deferred();
    installClipboard({ writeText: vi.fn(() => write.promise) });
    const { container, unmount } = renderMessage(message);
    fireEvent.click(scope(container).getByRole('button', { name: 'Copy' }));
    unmount();
    await act(async () => { write[settle](new Error('Late failure')); });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('clears its feedback timer on unmount', async () => {
    installClipboard({ writeText: vi.fn().mockResolvedValue() });
    const { container, unmount } = renderMessage(message);
    await act(async () => { fireEvent.click(scope(container).getByRole('button', { name: 'Copy' })); });
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

it('keeps copying independent from server-backed action availability', async () => {
  installClipboard({ writeText: vi.fn().mockResolvedValue() });
  renderMessage({ ...initialMessage, id: 'local-user-1', metadata: { optimistic: true } });
  expect(screen.getByRole('button', { name: 'Regenerate' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy' })); });
  expect(screen.getByRole('button', { name: 'Copied' })).toBeEnabled();
});
