import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import HistoryModal from './HistoryModal';

vi.mock('../lib/gotrue_client', () => ({ getGoTrueAccessToken: vi.fn(async () => '') }));

const thread = { id: 'synthetic-thread', title: 'Synthetic saved conversation' };
const response = (data) => new Response(JSON.stringify({ ok: true, data, error: null }), {
  status: 200, headers: { 'Content-Type': 'application/json' },
});
const deferred = () => {
  let resolve;
  const promise = new Promise((accept) => { resolve = accept; });
  return { promise, resolve };
};
const modal = () => <HistoryModal isOpen onClose={() => {}} principalKey="synthetic-history-owner" />;

beforeEach(() => { window.__VIOLA_API_KEY__ = 'synthetic-history-fixture'; });
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete window.__VIOLA_API_KEY__;
});

describe('Saved history stalled-read recovery through the actual adapter', () => {
  it('offers recovery when the saved-chat list never answers', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    await act(async () => render(modal()));
    expect(screen.getByText('Loading saved chats...')).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load saved chats');
    expect(screen.getByRole('button', { name: 'Retry saved chats' })).toBeEnabled();
  });

  it('offers recovery when a selected conversation never answers', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((path) => path === '/v1/chat/threads'
      ? Promise.resolve(response({ threads: [thread] })) : new Promise(() => {})));
    await act(async () => render(modal()));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    expect(screen.getByText('Loading conversation...')).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this conversation');
    expect(screen.getByRole('button', { name: 'Retry conversation' })).toBeEnabled();
  });

  it('keeps a successful empty saved-chat list distinct from failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ threads: [] })));
    await act(async () => render(modal()));
    expect(screen.getByText('No saved chats yet.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('displays a successfully loaded saved answer', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path) => response(path === '/v1/chat/threads'
      ? { threads: [thread] }
      : { thread, messages: [{ id: 'synthetic-answer', role: 'assistant', content: 'Synthetic saved answer' }] })));
    await act(async () => render(modal()));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    expect(screen.getByText('Synthetic saved answer')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('keeps a genuine serialized refusal visible', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'synthetic_unavailable', message: 'Synthetic unavailable history' } }), { status: 503 })));
    await act(async () => render(modal()));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load saved chats');
    expect(screen.getByRole('button', { name: 'Retry saved chats' })).toBeEnabled();
    expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
  });

  it('preserves the existing consent-required explanation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'consent_required', message: 'Synthetic consent required' } }), { status: 403 })));
    await act(async () => render(modal()));
    expect(screen.getByRole('alert')).toHaveTextContent('enable Cloud Sync');
    expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
  });

  it('ignores an older list settling after Close and a newer opening', async () => {
    const old = deferred();const newest = { id: 'newest-thread', title: 'Current saved conversation' };
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(response({ threads: [newest] })));
    const view = render(modal());await act(async () => {});
    await act(async () => view.rerender(<HistoryModal isOpen={false} onClose={() => {}} principalKey="synthetic-history-owner" />));
    await act(async () => view.rerender(modal()));
    await act(async () => old.resolve(response({ threads: [thread] })));
    expect(screen.getByRole('button', { name: newest.title })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: thread.title })).not.toBeInTheDocument();
  });

  it('ignores an older selected-conversation read after the user selects another', async () => {
    const old = deferred();const newest = { id: 'newest-thread', title: 'Current saved conversation' };
    vi.stubGlobal('fetch', vi.fn((path) => {
      if (path === '/v1/chat/threads') return Promise.resolve(response({ threads: [thread, newest] }));
      if (path.endsWith('/synthetic-thread')) return old.promise;
      return Promise.resolve(response({ thread: newest, messages: [{ id: 'newest-answer', role: 'assistant', content: 'Current selected answer' }] }));
    }));
    await act(async () => render(modal()));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: newest.title })));
    await act(async () => old.resolve(response({ thread, messages: [{ id: 'old-answer', role: 'assistant', content: 'Stale selected answer' }] })));
    expect(screen.getByText('Current selected answer')).toBeInTheDocument();
    expect(screen.queryByText('Stale selected answer')).not.toBeInTheDocument();
  });

  it('retries an expired list read and ignores its late successful payload', async () => {
    vi.useFakeTimers();const old = deferred();
    const fetch = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(response({ threads: [] }));vi.stubGlobal('fetch', fetch);
    await act(async () => render(modal()));
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry saved chats' })));
    await act(async () => old.resolve(response({ threads: [thread] })));
    expect(fetch).toHaveBeenCalledTimes(2);expect(screen.getByText('No saved chats yet.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: thread.title })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('retries an expired conversation while preserving selection and rejecting its late answer', async () => {
    vi.useFakeTimers();const old = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(response({ threads: [thread] })).mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(response({ thread, messages: [] }));vi.stubGlobal('fetch', fetch);
    await act(async () => render(modal()));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('button', { name: thread.title })).toHaveAttribute('aria-pressed', 'true');
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry conversation' })));
    await act(async () => old.resolve(response({ thread, messages: [{ id: 'late-answer', role: 'assistant', content: 'Expired answer' }] })));
    expect(fetch).toHaveBeenCalledTimes(3);expect(screen.getByText('No messages in this conversation yet.')).toBeInTheDocument();
    expect(screen.queryByText('Expired answer')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: thread.title })).toHaveAttribute('aria-pressed', 'true');
  });

  it.each(['list', 'conversation'])('checks the %s response deadline before a delayed timer task executes', async (kind) => {
    vi.useFakeTimers();const pending = deferred();
    vi.stubGlobal('fetch', vi.fn((path) => kind === 'conversation' && path === '/v1/chat/threads'
      ? Promise.resolve(response({ threads: [thread] })) : pending.promise));
    await act(async () => render(modal()));
    if (kind === 'conversation') await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    vi.spyOn(performance, 'now').mockReturnValue(15000);
    await act(async () => pending.resolve(response(kind === 'list' ? { threads: [thread] }
      : { thread, messages: [{ id: 'late-answer', role: 'assistant', content: 'Expired answer' }] })));
    expect(screen.getByRole('alert')).toHaveTextContent('in time');
    expect(screen.getByRole('button', { name: kind === 'list' ? 'Retry saved chats' : 'Retry conversation' })).toBeEnabled();
    expect(screen.queryByText('Expired answer')).not.toBeInTheDocument();
    if (kind === 'list') expect(screen.queryByRole('button', { name: thread.title })).not.toBeInTheDocument();
  });

  it.each(['list', 'conversation'])('coalesces duplicate same-turn %s Retry clicks into one read', async (kind) => {
    vi.useFakeTimers();const initial = deferred();const retry = deferred();
    const fetch = vi.fn();
    if (kind === 'conversation') fetch.mockResolvedValueOnce(response({ threads: [thread] }));
    fetch.mockReturnValueOnce(initial.promise).mockReturnValueOnce(retry.promise);vi.stubGlobal('fetch', fetch);
    await act(async () => render(modal()));
    if (kind === 'conversation') await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    const button = screen.getByRole('button', { name: kind === 'list' ? 'Retry saved chats' : 'Retry conversation' });
    await act(async () => { fireEvent.click(button); fireEvent.click(button); });
    expect(fetch).toHaveBeenCalledTimes(kind === 'list' ? 2 : 3);
    await act(async () => retry.resolve(response(kind === 'list' ? { threads: [] } : { thread, messages: [] })));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('retires an older selected-reader deadline without expiring its replacement', async () => {
    vi.useFakeTimers();const first = deferred();const second = deferred();const other = { id: 'other', title: 'Other saved conversation' };
    vi.stubGlobal('fetch', vi.fn((path) => {
      if (path === '/v1/chat/threads') return Promise.resolve(response({ threads: [thread, other] }));
      return path.endsWith('/synthetic-thread') ? first.promise : second.promise;
    }));
    await act(async () => render(modal()));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: other.title })));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText('Loading conversation...')).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async () => first.resolve(response({ thread, messages: [{ role: 'assistant', content: 'Stale answer' }] })));
    await act(async () => second.resolve(response({ thread: other, messages: [{ role: 'assistant', content: 'Current answer' }] })));
    expect(screen.getByText('Current answer')).toBeInTheDocument();expect(screen.queryByText('Stale answer')).not.toBeInTheDocument();
  });

  it('starts a fresh list deadline after Close and reopen', async () => {
    vi.useFakeTimers();const first = deferred();const second = deferred();
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
    const view = render(modal());await act(async () => vi.advanceTimersByTimeAsync(10000));
    await act(async () => view.rerender(<HistoryModal isOpen={false} onClose={() => {}} principalKey="synthetic-history-owner" />));
    await act(async () => view.rerender(modal()));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText('Loading saved chats...')).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async () => second.resolve(response({ threads: [] })));
    expect(screen.getByText('No saved chats yet.')).toBeInTheDocument();
  });

  it('keeps the list and selected-conversation deadlines independent', async () => {
    vi.useFakeTimers();const selected = deferred();
    vi.stubGlobal('fetch', vi.fn((path) => path === '/v1/chat/threads'
      ? Promise.resolve(response({ threads: [thread] })) : selected.promise));
    await act(async () => render(modal()));await act(async () => vi.advanceTimersByTimeAsync(10000));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: thread.title })));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText('Loading conversation...')).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async () => selected.resolve(response({ thread, messages: [] })));
    expect(screen.getByText('No messages in this conversation yet.')).toBeInTheDocument();
  });

  it('clears the pending deadline on unmount', async () => {
    vi.useFakeTimers();vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    const view = render(modal());await act(async () => {});expect(vi.getTimerCount()).toBeGreaterThan(0);
    view.unmount();await act(async () => vi.advanceTimersByTimeAsync(0));expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps a successful read free of an expired timer under StrictMode', async () => {
    vi.useFakeTimers();const first = deferred();
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(first.promise).mockResolvedValueOnce(response({ threads: [thread] })));
    await act(async () => render(<StrictMode>{modal()}</StrictMode>));
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('button', { name: thread.title })).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async () => first.resolve(response({ threads: [] })));
    expect(screen.getByRole('button', { name: thread.title })).toBeInTheDocument();
  });

  it('preserves recent-activity filtering and Clear/Close callbacks after the saved-list deadline', async () => {
    vi.useFakeTimers();const clear = vi.fn();const close = vi.fn();vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    await act(async () => render(<HistoryModal isOpen onClose={close} onClearHistory={clear}
      history={[{ role: 'user', content: 'Synthetic recent command' }, { role: 'assistant', content: 'Synthetic recent response' }]} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Filter by commands' })));
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('button', { name: 'Filter by commands' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Synthetic recent command')).toBeInTheDocument();expect(screen.queryByText('Synthetic recent response')).not.toBeInTheDocument();
    expect(clear).not.toHaveBeenCalled();expect(close).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Clear recent activity' })));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Close history modal' })));
    expect(clear).toHaveBeenCalledTimes(1);expect(close).toHaveBeenCalledTimes(1);
  });

});
