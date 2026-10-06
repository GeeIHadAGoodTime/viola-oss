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

describe('MemoryPanel desktop draft ownership through authFetch', () => {
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

  it('keeps a newer unsaved draft editable when an older save succeeds', async () => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    await mount();
    type('Submitted note A');
    await save();
    type('Newer unsaved note B');
    expect(JSON.parse(writes.mock.calls[0][1].body).content).toBe('Submitted note A');
    await act(async () => old.resolve(jsonResponse({ ok: true, audit_id: 'synthetic-audit', path: 'VIOLA.md' })));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Newer unsaved note B');
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  it('an old save cannot close the editor from a newer opening', async () => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    const view = await mount();
    type('Submitted note A');
    await save();
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    type('Draft in the newer opening');
    await act(async () => old.resolve(jsonResponse({ ok: true, audit_id: 'synthetic-audit', path: 'VIOLA.md' })));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Draft in the newer opening');
  });

  it('retains an unchanged accepted save and its request body', async () => {
    await mount();
    type('Accepted synthetic note');
    await save();
    expect(screen.queryByRole('textbox', { name: 'Edit VIOLA.md' })).not.toBeInTheDocument();
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Accepted synthetic note');
    expect(stored).toBe('Accepted synthetic note');
  });

  it('keeps the draft and current error when the server refuses the save', async () => {
    writes.mockResolvedValueOnce(jsonResponse(null, 500));
    await mount();
    type('Retain this synthetic draft');
    await save();
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Retain this synthetic draft');
    expect(screen.getByText('Synthetic write refused')).toBeInTheDocument();
    expect(stored).toBe('Original synthetic note\n');
  });

  it('saves the later draft on a second explicit Save after the first acknowledgement', async () => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    await mount();
    type('First submitted note');
    await save();
    type('Second owned draft');
    await act(async () => old.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    await save();
    expect(writes).toHaveBeenCalledTimes(2);
    expect(JSON.parse(writes.mock.calls[1][1].body).content).toBe('Second owned draft');
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Second owned draft');
  });

  it('Cancel restores the acknowledged baseline after retaining a later edit', async () => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    await mount();
    type('Acknowledged baseline');
    await save();
    type('Unsaved later draft');
    await act(async () => old.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Acknowledged baseline');
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Acknowledged baseline');
  });

  it('retains an inserted template while an earlier Save settles', async () => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    await mount();
    type('Submitted note');
    await save();
    fireEvent.click(screen.getByRole('button', { name: '+ Always do' }));
    const newerDraft = screen.getByRole('textbox', { name: 'Edit VIOLA.md' }).value;
    expect(newerDraft).toContain('## Always do');
    await act(async () => old.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue(newerDraft);
  });

  it.each([200, 500])('a retired Save %i cannot release or report over a newer pending Save', async status => {
    const old = deferred(), current = deferred();
    writes.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
    const view = await mount();
    type('Old opening');
    await save();
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    type('Current opening');
    await save();
    await act(async () => old.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' }, status)));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Current opening');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.queryByText('Synthetic write refused')).not.toBeInTheDocument();
    await act(async () => current.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Current opening');
  });

  it.each(['button', 'Escape', 'backdrop'])('retires Save immediately on %s close intent', async method => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    const onClose = vi.fn();
    render(<MemoryPanel isOpen onClose={onClose} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    type('Closing draft');
    await save();
    if (method === 'button') fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    else if (method === 'Escape') fireEvent.keyDown(document, { key: 'Escape' });
    else fireEvent.click(screen.getByTestId('memory-panel'));
    expect(onClose).toHaveBeenCalledTimes(1);
    const before = entryReads.mock.calls.length;
    await act(async () => old.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    expect(entryReads).toHaveBeenCalledTimes(before);
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Closing draft');
  });

  it.each([200, 500])('ignores a previous opening read %i after a new opening edits', async status => {
    const old = deferred();
    reads.mockReturnValueOnce(old.promise);
    const view = render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(1));
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    type('Current draft after reopening');
    await act(async () => old.resolve(jsonResponse({ path: 'VIOLA.md', content: 'Stale old snapshot' }, status)));
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Current draft after reopening');
    expect(screen.queryByText('Synthetic write refused')).not.toBeInTheDocument();
  });

  it('ignores a retired post-save entries refresh after reopening', async () => {
    const oldEntries = deferred();
    entryReads.mockResolvedValueOnce(jsonResponse({ index: { entries: [] }, topics: [] })).mockReturnValueOnce(oldEntries.promise);
    const view = await mount();
    type('Accepted old opening');
    await save();
    await waitFor(() => expect(entryReads).toHaveBeenCalledTimes(2));
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    await act(async () => oldEntries.resolve(jsonResponse(null, 500)));
    expect(screen.queryByText('Synthetic write refused')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    type('New editable draft');
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('New editable draft');
  });

  it.each([200, 500])('ignores late Save %i after unmount without a new follow-up read', async status => {
    const old = deferred();
    writes.mockReturnValueOnce(old.promise);
    const view = await mount();
    type('Unmounted draft');
    await save();
    const before = entryReads.mock.calls.length;
    view.unmount();
    await act(async () => old.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' }, status)));
    expect(entryReads).toHaveBeenCalledTimes(before);
  });

  it('excludes same-turn duplicate Save dispatch', async () => {
    writes.mockReturnValueOnce(new Promise(() => {}));
    await mount();
    type('Only one submitted draft');
    await act(async () => {
      const button = screen.getByRole('button', { name: 'Save' });
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(writes).toHaveBeenCalledTimes(1);
  });

  it('preserves a current Save under StrictMode effect replay', async () => {
    render(<StrictMode><MemoryPanel isOpen onClose={vi.fn()} /></StrictMode>);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    type('StrictMode accepted note');
    await save();
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('StrictMode accepted note');
    expect(writes).toHaveBeenCalledTimes(1);
  });


  it('submits the actual latest draft when editing and Save share a React batch', async () => {
    await mount();
    await act(async () => {
      type('Latest batched draft');
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });
    expect(JSON.parse(writes.mock.calls[0][1].body).content).toBe('Latest batched draft');
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Latest batched draft');
  });


  it('preserves a genuine current opening load error', async () => {
    reads.mockResolvedValueOnce(jsonResponse(null, 500));
    render(<MemoryPanel isOpen onClose={vi.fn()} />);
    expect(await screen.findByText('Synthetic write refused')).toBeInTheDocument();
    expect(writes).not.toHaveBeenCalled();
  });

  it('keeps the new opening memory entries after an older post-save read completes', async () => {
    const old = deferred();
    const entry = text => ({ index: { entries: [{ id: text, kind: 'line', line_number: 1, markdown: text, text }] }, topics: [] });
    entryReads.mockResolvedValueOnce(jsonResponse(entry('Initial snapshot'))).mockReturnValueOnce(old.promise).mockResolvedValueOnce(jsonResponse(entry('Current snapshot')));
    const view = await mount();
    type('Saved before closing');
    await save();
    await waitFor(() => expect(entryReads).toHaveBeenCalledTimes(2));
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Memory' }));
    expect(screen.getByText('Current snapshot')).toBeInTheDocument();
    await act(async () => old.resolve(jsonResponse(entry('Stale snapshot'))));
    expect(screen.getByText('Current snapshot')).toBeInTheDocument();
    expect(screen.queryByText('Stale snapshot')).not.toBeInTheDocument();
  });

  it('keeps the current opening Workbench list after an older opening load completes', async () => {
    const old = deferred();
    const files = name => ({ files: [{ name, size: 12, mime: 'text/plain', modified_at: '2026-10-06T00:00:00Z' }] });
    workbenchReads.mockReturnValueOnce(old.promise).mockResolvedValueOnce(jsonResponse(files('current-synthetic.txt')));
    const view = render(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(workbenchReads).toHaveBeenCalledTimes(1));
    view.rerender(<MemoryPanel isOpen={false} onClose={vi.fn()} />);
    view.rerender(<MemoryPanel isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Workbench' }));
    expect(screen.getByText('current-synthetic.txt')).toBeInTheDocument();
    await act(async () => old.resolve(jsonResponse(files('stale-synthetic.txt'))));
    expect(screen.getByText('current-synthetic.txt')).toBeInTheDocument();
    expect(screen.queryByText('stale-synthetic.txt')).not.toBeInTheDocument();
  });


  it('keeps Save pending after a sibling Workbench action releases shared busy state', async () => {
    const saveResult = deferred();
    writes.mockReturnValueOnce(saveResult.promise);
    uploads.mockResolvedValueOnce(jsonResponse(null, 500));
    const view = await mount();
    type('Still pending document');
    await save();
    fireEvent.click(screen.getByRole('button', { name: 'Workbench' }));
    const file = new File(['synthetic only'], 'synthetic-fixture.txt', { type: 'text/plain' });
    fireEvent.change(view.container.querySelector('input[type="file"]'), { target: { files: [file] } });
    await screen.findByText('Synthetic write refused');
    expect(uploads).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Viola' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'Edit VIOLA.md' })).toHaveValue('Still pending document');
    await act(async () => saveResult.resolve(jsonResponse({ ok: true, path: 'VIOLA.md' })));
    expect(screen.getByTestId('viola-md-rendered')).toHaveTextContent('Still pending document');
  });

});
