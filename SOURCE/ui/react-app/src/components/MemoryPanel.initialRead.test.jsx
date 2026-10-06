import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../test/test-utils';
import MemoryPanel from './MemoryPanel';

const jsonResponse = (data, status = 200) => new Response(JSON.stringify(
  status === 200 ? { ok: true, error: null, data } : { ok: false, error: { code: 'memory_write_failed', message: 'Synthetic write refused' }, data: null },
), { status, headers: { 'Content-Type': 'application/json' } });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

describe('MemoryPanel initial document read admission through authFetch', () => {
  let writes, reads, entryReads, workbenchReads, uploads, stored, unexpected;
  beforeEach(() => {
    window.viola = {};
    window.__VIOLA_API_KEY__ = 'synthetic-memory-fixture';
    window.history.replaceState({}, '', '/app');
    stored = 'Original synthetic note\n';
    unexpected = [];
    reads = vi.fn(async () => jsonResponse({ path: 'VIOLA.md', content: stored }));
    entryReads = vi.fn(async () => jsonResponse({ index: { entries: [] }, topics: [] }));
    workbenchReads = vi.fn(async () => jsonResponse({ files: [] }));
    uploads = vi.fn(async () => jsonResponse({ uploaded: true }));
    writes = vi.fn(async (_url, options) => {
      stored = JSON.parse(options.body).content;
      return jsonResponse({ ok: true, audit_id: 'synthetic-audit', path: 'VIOLA.md' });
    });
    vi.stubGlobal('fetch', async (url, options = {}) => {
      const method = options.method || 'GET';
      if (url === '/api/memory/viola' && method === 'GET') return reads();
      if (url === '/api/memory/viola' && method === 'PUT') return writes(url, options);
      if (url === '/api/memory/entries') return entryReads();
      if (url === '/api/workbench/files' && method === 'POST') return uploads(url, options);
      if (url === '/api/workbench/files') return workbenchReads();
      unexpected.push(`${method} ${url}`);
      throw new Error(`Unexpected synthetic request: ${method} ${url}`);
    });
  });
  afterEach(() => {
    expect(unexpected).toEqual([]);
    vi.unstubAllGlobals();
    delete window.viola;
    delete window.__VIOLA_API_KEY__;
  });
  const mount = async () => {
    const view = render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    return view;
  };
  const type = value => fireEvent.change(screen.getByRole('textbox', { name: 'Edit VIOLA.md' }), { target: { value } });
  const save = async () => act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })); });

  it('keeps template admission closed while the initial document is unknown', async () => {
    const pending = deferred(); reads.mockReturnValueOnce(pending.promise);
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '+ Always do' }));
    expect(screen.queryByRole('textbox', { name: 'Edit VIOLA.md' })).not.toBeInTheDocument();
    expect(writes).not.toHaveBeenCalled();
    await act(async () => pending.resolve(jsonResponse({ content: 'Existing note must be loaded first' })));
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '+ Always do' }));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' }).value).toContain('Existing note must be loaded first');
  });

  it('a sibling read failure cannot enable editing of an unknown document', async () => {
    const pending = deferred(); reads.mockReturnValueOnce(pending.promise);
    workbenchReads.mockResolvedValueOnce(jsonResponse(null, 500));
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await screen.findByText('Synthetic write refused');
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.queryByRole('textbox', { name: 'Edit VIOLA.md' })).not.toBeInTheDocument();
    expect(writes).not.toHaveBeenCalled();
    await act(async () => pending.resolve(jsonResponse({ content: 'Real initial baseline' })));
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Real initial baseline');
  });

  it('a failed initial document read stays non-editable and can be retried', async () => {
    reads.mockResolvedValueOnce(jsonResponse(null, 500));
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await screen.findByText('Synthetic write refused');
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry VIOLA.md' }));
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    expect(screen.queryByText('Synthetic write refused')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Original synthetic note\n');
  });

  it('a successfully read empty document is a known editable baseline', async () => {
    stored = ''; await mount();
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('');
    type('First explicit content'); await save();
    expect(stored).toBe('First explicit content');
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('First explicit content');
  });

  it('keeps later editing and templates available during a save of a known document', async () => {
    const pending = deferred(); writes.mockReturnValueOnce(pending.promise);
    await mount(); type('Submitted content'); await save();
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeEnabled();
    type('Later unsaved content');
    await act(async () => pending.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Later unsaved content');
  });

  it.each([
    ['missing content', {}], ['null content', { content: null }], ['numeric content', { content: 0 }],
    ['array content', { content: [] }], ['boolean content', { content: false }],
  ])('keeps a malformed %s read non-editable until a valid Retry succeeds', async (_label, data) => {
    reads.mockResolvedValueOnce(jsonResponse(data));
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await screen.findByText('VIOLA.md response did not contain document content.');
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeDisabled();
    expect(writes).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry VIOLA.md' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Original synthetic note');
  });

  it('coalesces repeated same-turn Retry intent and keeps editing closed until settlement', async () => {
    const retry = deferred();
    reads.mockResolvedValueOnce(jsonResponse(null, 500)).mockReturnValueOnce(retry.promise);
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    const button = await screen.findByRole('button', { name: 'Retry VIOLA.md' });
    await act(async () => { fireEvent.click(button); fireEvent.click(button); });
    expect(reads).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeDisabled();
    await act(async () => retry.resolve(jsonResponse({ content: 'Current retry baseline' })));
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled();
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Current retry baseline');
  });

  it.each(['success', 'failure'])('retires an old opening Retry %s without changing the new draft', async outcome => {
    const retry = deferred();
    reads.mockResolvedValueOnce(jsonResponse(null, 500)).mockReturnValueOnce(retry.promise);
    const view = render(<MemoryPanel isOpen onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry VIOLA.md' }));
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Edit' })); type('New opening owns this draft');
    await act(async () => retry.resolve(outcome === 'success' ? jsonResponse({ content: 'Retired read' }) : jsonResponse(null, 500)));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('New opening owns this draft');
    expect(screen.queryByRole('button', { name: 'Retry VIOLA.md' })).not.toBeInTheDocument();
    expect(screen.queryByText('Synthetic write refused')).not.toBeInTheDocument();
  });

  it('a Retry failure remains visible and does not enable unknown-document actions', async () => {
    reads.mockResolvedValueOnce(jsonResponse(null, 500)).mockRejectedValueOnce(new Error('Synthetic retry disconnected'));
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry VIOLA.md' }));
    await screen.findByText('Synthetic retry disconnected');
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+ Always do' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Retry VIOLA.md' })).toBeEnabled();
    expect(writes).not.toHaveBeenCalled();
  });

  it('reopening with a previous editor waits for the new opening baseline', async () => {
    const view = await mount(); type('Abandoned prior draft');
    const pending = deferred(); reads.mockReturnValueOnce(pending.promise);
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.queryByRole('textbox', { name: 'Edit VIOLA.md' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(writes).not.toHaveBeenCalled();
    await act(async () => pending.resolve(jsonResponse({ content: 'New opening baseline' })));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('New opening baseline');
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  it.each(['close', 'unmount'])('retires a delayed read on immediate %s intent', async ending => {
    const pending = deferred(); reads.mockReturnValueOnce(pending.promise);
    const onClose = vi.fn(); const view = render(<MemoryPanel isOpen onClose={onClose} />);
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(1));
    if (ending === 'unmount') view.unmount();
    else fireEvent.keyDown(document, { key: 'Escape' });
    await act(async () => pending.resolve(jsonResponse({ content: 'Retired initial baseline' })));
    if (ending === 'close') {
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
      expect(screen.queryByText('Retired initial baseline')).not.toBeInTheDocument();
    }
    expect(writes).not.toHaveBeenCalled();
  });

  it('StrictMode admits only the current opening read', async () => {
    const retired = deferred(); reads.mockReturnValueOnce(retired.promise);
    render(<StrictMode><MemoryPanel isOpen onClose={vi.fn()} /></StrictMode>);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    expect(reads).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' })); type('Current StrictMode draft');
    await act(async () => retired.resolve(jsonResponse({ content: 'Retired StrictMode read' })));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Current StrictMode draft');
  });
});
