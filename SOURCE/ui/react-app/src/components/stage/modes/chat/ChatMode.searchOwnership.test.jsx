import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '../../../../test/test-utils';
import ChatMode from './ChatMode';

vi.mock('../../../../hooks/useWebSocket', () => ({ useWebSocket: () => ({}) }));
const response = data => new Response(JSON.stringify({ ok: true, data, error: null }), {
  status: 200, headers: { 'Content-Type': 'application/json' },
});
const deferred = () => {
  let resolve;
  const promise = new Promise(accept => { resolve = accept; });
  return { promise, resolve };
};
const stored = { id: 'stored-thread', title: 'Stored synthetic conversation', model: 'model-a' };
const alpha = { id: 'alpha-thread', title: 'Alpha synthetic result', model: 'model-a' };
const beta = { id: 'beta-thread', title: 'Beta synthetic result', model: 'model-a' };

describe('Conversation search ownership through the actual adapter', () => {
  let searches, unexpected, defaultList, calls;
  beforeEach(() => {
    searches = new Map();unexpected = [];calls = [];
    defaultList = vi.fn(async () => response({ threads: [stored] }));
    window.viola = {};window.__VIOLA_API_KEY__ = 'synthetic-search-fixture';
    vi.useFakeTimers();
    vi.stubGlobal('fetch', async path => {
      calls.push(path);
      if (path === '/v1/chat/models') return response({ current_model: 'model-a', provider: 'Synthetic', models: ['model-a'] });
      if (path === '/v1/chat/threads') return defaultList();
      const selected = [stored, alpha, beta].find(thread => path === `/v1/chat/threads/${thread.id}`);
      if (selected) return response({ thread: selected, messages: [], active_stream_ids: [] });
      if (path.startsWith('/v1/chat/threads?')) {
        const query = new URLSearchParams(path.split('?')[1]).get('search');
        if (searches.has(query)) return searches.get(query).promise;
      }
      unexpected.push(path);throw new Error('Unexpected synthetic route');
    });
  });
  afterEach(() => {
    expect(unexpected).toEqual([]);vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals();
    delete window.viola;delete window.__VIOLA_API_KEY__;
  });
  async function mount(principalKey = 'synthetic-search-owner') {
    let view;
    await act(async () => { view = render(<ChatMode principalKey={principalKey} />); });
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByDisplayValue('Stored synthetic conversation')).toBeInTheDocument();
    return view;
  }
  async function query(value) {
    const request = deferred();searches.set(value, request);
    await act(async () => fireEvent.change(screen.getByRole('textbox', { name: 'Search chats' }), { target: { value } }));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    return request;
  }
  it('keeps results for the newer visible query after an older search settles late', async () => {
    await mount();
    const older = await query('alpha');const newer = await query('beta');
    await act(async () => newer.resolve(response({ threads: [beta] })));
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    await act(async () => older.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('textbox', { name: 'Search chats' })).toHaveValue('beta');
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('displays successful searches that settle in issue order', async () => {
    await mount();const first = await query('alpha');
    await act(async () => first.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('button', { name: alpha.title, exact: true })).toBeInTheDocument();
    const second = await query('beta');await act(async () => second.resolve(response({ threads: [beta] })));
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('preserves selected conversation, confirmed model and unsent draft during a search', async () => {
    await mount();
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Viola' }), { target: { value: 'Keep this unsent draft' } });
    const request = await query('beta');await act(async () => request.resolve(response({ threads: [beta] })));
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('model-a');
    expect(screen.getByRole('textbox', { name: 'Message Viola' })).toHaveValue('Keep this unsent draft');
  });
  it('keeps the existing principal retirement when a search resolves after switching account', async () => {
    const view = await mount();const older = await query('alpha');
    await act(async () => view.rerender(<ChatMode principalKey="another-synthetic-owner" />));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    await act(async () => older.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('textbox', { name: 'Search chats' })).toHaveValue('');
    expect(screen.getByRole('button', { name: stored.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('retires an older response immediately when the visible query changes before debounce', async () => {
    await mount();const older = await query('alpha');
    await act(async () => fireEvent.change(screen.getByRole('textbox', { name: 'Search chats' }), { target: { value: 'beta' } }));
    await act(async () => older.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('textbox', { name: 'Search chats' })).toHaveValue('beta');
    expect(screen.getByRole('button', { name: stored.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('keeps the latest A result after A then B then A returns to the same query', async () => {
    await mount();const firstA = await query('alpha');const middleB = await query('beta');const lastA = await query('alpha');
    const newest = { ...alpha, title: 'Newest Alpha synthetic result' };
    await act(async () => lastA.resolve(response({ threads: [newest] })));
    expect(screen.getByRole('button', { name: newest.title, exact: true })).toBeInTheDocument();
    await act(async () => middleB.resolve(response({ threads: [beta] })));
    await act(async () => firstA.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('textbox', { name: 'Search chats' })).toHaveValue('alpha');
    expect(screen.getByRole('button', { name: newest.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('keeps the unfiltered result after clearing a pending search', async () => {
    await mount();const older = await query('alpha');
    await act(async () => fireEvent.change(screen.getByRole('textbox', { name: 'Search chats' }), { target: { value: '' } }));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByRole('button', { name: stored.title, exact: true })).toBeInTheDocument();
    await act(async () => older.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('textbox', { name: 'Search chats' })).toHaveValue('');
    expect(screen.getByRole('button', { name: stored.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('ignores an older search consent refusal after the current search succeeds', async () => {
    await mount();const older = await query('alpha');const newer = await query('beta');
    await act(async () => newer.resolve(response({ threads: [beta] })));
    await act(async () => older.resolve(new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'consent_required', message: 'Synthetic older consent refusal' } }), { status: 403 })));
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByTestId('chat-consent-required')).not.toBeInTheDocument();
  });
  it('preserves a current search result when an older request fails generically', async () => {
    await mount();const older = await query('alpha');const newer = await query('beta');
    await act(async () => newer.resolve(response({ threads: [beta] })));
    await act(async () => older.resolve(new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'service_unavailable', message: 'Synthetic older refusal' } }), { status: 503 })));
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByTestId('chat-consent-required')).not.toBeInTheDocument();
  });

  it('preserves initial conversation opening when a same-query refresh finishes before the boot request', async () => {
    const initial = deferred();defaultList.mockReturnValueOnce(initial.promise);
    await act(async () => render(<ChatMode principalKey="synthetic-search-owner" />));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByRole('button', { name: stored.title, exact: true })).toBeInTheDocument();
    await act(async () => initial.resolve(response({ threads: [stored] })));
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('model-a');
  });
  it('keeps the latest consent error after an older successful search arrives', async () => {
    await mount();const older = await query('alpha');const current = await query('beta');
    await act(async () => current.resolve(new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'consent_required', message: 'Synthetic current consent refusal' } }), { status: 403 })));
    expect(screen.getByTestId('chat-consent-required')).toBeInTheDocument();
    await act(async () => older.resolve(response({ threads: [alpha] })));
    expect(screen.getByTestId('chat-consent-required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('recovers from current consent refusal with a later successful query', async () => {
    await mount();const refused = await query('alpha');
    await act(async () => refused.resolve(new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'consent_required', message: 'Synthetic current refusal' } }), { status: 403 })));
    expect(screen.getByTestId('chat-consent-required')).toBeInTheDocument();
    const current = await query('beta');await act(async () => current.resolve(response({ threads: [beta] })));
    expect(screen.queryByTestId('chat-consent-required')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
  });
  it('keeps the existing generic-refusal behavior and confirmed conversation', async () => {
    await mount();const current = await query('beta');
    await act(async () => current.resolve(new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'service_unavailable', message: 'Synthetic current refusal' } }), { status: 503 })));
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: stored.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByTestId('chat-consent-required')).not.toBeInTheDocument();
  });
  it('accepts a genuine empty result from the current search', async () => {
    await mount();const current = await query('beta');
    await act(async () => current.resolve(response({ threads: [] })));
    expect(screen.queryByRole('button', { name: stored.title, exact: true })).not.toBeInTheDocument();
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.queryByTestId('chat-consent-required')).not.toBeInTheDocument();
  });
  it('preserves the180 millisecond search debounce', async () => {
    await mount();searches.set('alpha', deferred());
    await act(async () => fireEvent.change(screen.getByRole('textbox', { name: 'Search chats' }), { target: { value: 'alpha' } }));
    await act(async () => vi.advanceTimersByTimeAsync(179));
    expect(calls.filter(path => path.includes('?search='))).toEqual([]);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(calls.filter(path => path.includes('?search='))).toEqual(['/v1/chat/threads?search=alpha']);
  });

  it('initializes from the latest admitted same-query snapshot instead of a retired list payload', async () => {
    const initial = deferred();defaultList.mockReturnValueOnce(initial.promise).mockResolvedValueOnce(response({ threads: [beta] }));
    await act(async () => render(<ChatMode principalKey="synthetic-search-owner" />));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    await act(async () => initial.resolve(response({ threads: [alpha] })));
    expect(screen.getByDisplayValue(beta.title)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('does not let a retired initial request clear current same-query consent refusal', async () => {
    const initial = deferred();defaultList.mockReturnValueOnce(initial.promise).mockResolvedValueOnce(new Response(JSON.stringify({
      ok: false, data: null, error: { code: 'consent_required', message: 'Synthetic current consent refusal' },
    }), { status: 403 }));
    await act(async () => render(<ChatMode principalKey="synthetic-search-owner" />));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByTestId('chat-consent-required')).toBeInTheDocument();
    await act(async () => initial.resolve(response({ threads: [alpha] })));
    expect(screen.getByTestId('chat-consent-required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });
  it('uses the admitted same-query snapshot when a retired initial read fails', async () => {
    const initial = deferred();defaultList.mockReturnValueOnce(initial.promise).mockResolvedValueOnce(response({ threads: [beta] }));
    await act(async () => render(<ChatMode principalKey="synthetic-search-owner" />));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    await act(async () => initial.resolve(new Response(JSON.stringify({ ok: false, data: null,
      error: { code: 'service_unavailable', message: 'Synthetic retired refusal' } }), { status: 503 })));
    expect(screen.getByDisplayValue(beta.title)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
  });
  it('retires StrictMode initial ownership and keeps the active query result', async () => {
    const initial = deferred();defaultList.mockReturnValueOnce(initial.promise);
    await act(async () => render(<StrictMode><ChatMode principalKey="synthetic-search-owner" /></StrictMode>));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    const current = await query('beta');await act(async () => current.resolve(response({ threads: [beta] })));
    await act(async () => initial.resolve(response({ threads: [alpha] })));
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
  });

  async function pendingBootstrapReads() {
    const initial = deferred();const refresh = deferred();
    defaultList.mockReturnValueOnce(initial.promise).mockReturnValueOnce(refresh.promise);
    let view;await act(async () => { view = render(<ChatMode principalKey="synthetic-search-owner" />); });
    await act(async () => vi.advanceTimersByTimeAsync(180));
    return { initial, refresh, view };
  }
  it('keeps bootstrap pending when the retired read settles before the current same-query read', async () => {
    const { initial, refresh } = await pendingBootstrapReads();
    await act(async () => initial.resolve(response({ threads: [stored] })));
    expect(screen.getByText('Loading chats...')).toBeInTheDocument();
    await act(async () => refresh.resolve(response({ threads: [stored] })));
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.queryByText('Loading chats...')).not.toBeInTheDocument();
  });
  it.each(['empty', 'consent', 'generic-error'])('settles retired bootstrap ownership on current %s outcome', async kind => {
    const { initial, refresh } = await pendingBootstrapReads();
    const reply = kind === 'empty' ? response({ threads: [] }) : new Response(JSON.stringify({
      ok: false, data: null, error: { code: kind === 'consent' ? 'consent_required' : 'service_unavailable', message: 'Synthetic current refusal' },
    }), { status: kind === 'consent' ? 403 : 503 });
    await act(async () => refresh.resolve(reply));
    expect(screen.queryByText('Loading chats...')).not.toBeInTheDocument();
    await act(async () => initial.resolve(response({ threads: [alpha] })));
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
    if (kind === 'consent') expect(screen.getByTestId('chat-consent-required')).toBeInTheDocument();
    else expect(screen.queryByTestId('chat-consent-required')).not.toBeInTheDocument();
  });
  it('releases old bootstrap admission when the query changes while its current read remains pending', async () => {
    const { initial, refresh } = await pendingBootstrapReads();
    const current = await query('beta');
    await act(async () => current.resolve(response({ threads: [beta] })));
    expect(screen.queryByText('Loading chats...')).not.toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: beta.title, exact: true })));
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Viola' }), { target: { value: 'Keep the current query draft' } });
    expect(screen.getByDisplayValue(beta.title)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeEnabled();
    await act(async () => { initial.resolve(response({ threads: [stored] }));refresh.resolve(response({ threads: [alpha] })); });
    expect(screen.getByRole('button', { name: beta.title, exact: true })).toBeInTheDocument();
    expect(screen.getByDisplayValue(beta.title)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message Viola' })).toHaveValue('Keep the current query draft');
  });
  it('releases the old bootstrap chain on principal change without touching the new owner', async () => {
    const { initial, refresh, view } = await pendingBootstrapReads();
    await act(async () => view.rerender(<ChatMode principalKey="new-synthetic-search-owner" />));
    await act(async () => vi.advanceTimersByTimeAsync(180));
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.queryByText('Loading chats...')).not.toBeInTheDocument();
    await act(async () => { initial.resolve(response({ threads: [alpha] }));refresh.resolve(response({ threads: [beta] })); });
    expect(screen.getByDisplayValue(stored.title)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: alpha.title, exact: true })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: beta.title, exact: true })).not.toBeInTheDocument();
  });

});
