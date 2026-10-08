import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../test/test-utils';
import MemoryPanel from './MemoryPanel';

const authFetchMock = vi.fn();
vi.mock('../hooks/useViolaApi', () => ({
  authFetch: (...args) => authFetchMock(...args),
}));

const response = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  text: async () => JSON.stringify(body),
});

describe('MemoryPanel desktop Workbench batch-upload recovery', () => {
  let stored;
  let postedNames;
  let failRefresh;

  beforeEach(() => {
    window.viola = {};
    window.history.replaceState({}, '', '/');
    stored = [];
    postedNames = [];
    failRefresh = false;
    authFetchMock.mockImplementation(async (path, options = {}) => {
      if (path === '/api/memory/viola') return response(200, { content: '' });
      if (path === '/api/memory/entries') return response(200, { index: { entries: [] }, topics: [] });
      if (path === '/api/workbench/files' && options.method === 'POST') {
        const file = options.body.get('file');
        postedNames.push(file.name);
        if (file.name.endsWith('.html')) {
          return response(400, { ok: false, error: { message: 'File type .html is not allowed.' } });
        }
        stored.push({ name: file.name, size: file.size, mime: file.type, modified_at: '2026-10-08T00:00:00Z' });
        return response(200, { ok: true, data: { name: file.name } });
      }
      if (path === '/api/workbench/files') {
        if (failRefresh && postedNames.length) {
          return response(503, { ok: false, error: { message: 'File listing unavailable.' } });
        }
        return response(200, { files: [...stored] });
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
    delete window.viola;
  });

  async function upload(files) {
    const view = render(<MemoryPanel isOpen onClose={() => {}} />);
    await waitFor(() => expect(authFetchMock).toHaveBeenCalledWith('/api/workbench/files'));
    fireEvent.click(screen.getByRole('button', { name: 'Workbench', exact: true }));
    await screen.findByText('No files in Workbench yet.');
    fireEvent.change(view.container.querySelector('input[type="file"]'), { target: { files } });
    return view;
  }

  it('shows a successful first file immediately when a later file is rejected, retaining the error', async () => {
    await upload([
      new File(['synthetic first file'], 'qa1482-first.txt', { type: 'text/plain' }),
      new File(['plain synthetic text'], 'qa1482-blocked.html', { type: 'text/html' }),
      new File(['not attempted'], 'qa1482-third.txt', { type: 'text/plain' }),
    ]);
    expect(await screen.findByText('File type .html is not allowed.')).toBeInTheDocument();
    expect(stored.map((file) => file.name)).toEqual(['qa1482-first.txt']);
    expect(await screen.findByText('qa1482-first.txt')).toBeInTheDocument();
    expect(screen.queryByText('qa1482-blocked.html')).not.toBeInTheDocument();
    expect(postedNames).toEqual(['qa1482-first.txt', 'qa1482-blocked.html']);
    expect(screen.getByRole('button', { name: 'Delete', exact: true })).toBeEnabled();
  });

  it('refreshes the list once after a wholly successful batch', async () => {
    await upload([
      new File(['one'], 'qa1482-one.txt', { type: 'text/plain' }),
      new File(['two'], 'qa1482-two.txt', { type: 'text/plain' }),
    ]);
    expect(await screen.findByText('qa1482-two.txt')).toBeInTheDocument();
    expect(screen.getByText('qa1482-one.txt')).toBeInTheDocument();
    expect(authFetchMock.mock.calls.filter(([path, opts]) => path === '/api/workbench/files' && !opts?.method)).toHaveLength(2);
  });

  it('preserves the rejection and discloses a failed refresh while recovering controls', async () => {
    failRefresh = true;
    const view = await upload([
      new File(['one'], 'qa1482-one.txt', { type: 'text/plain' }),
      new File(['plain text'], 'qa1482-blocked.html', { type: 'text/html' }),
    ]);
    expect(await screen.findByText(/File type \.html is not allowed\..*File list could not be refreshed\..*File listing unavailable\./)).toBeInTheDocument();
    expect(stored.map((file) => file.name)).toEqual(['qa1482-one.txt']);
    failRefresh = false;
    fireEvent.change(view.container.querySelector('input[type="file"]'), {
      target: { files: [new File(['plain text'], 'qa1482-blocked.html', { type: 'text/html' })] },
    });
    expect(await screen.findByText('qa1482-one.txt')).toBeInTheDocument();
    expect(screen.getByText('File type .html is not allowed.')).toBeInTheDocument();
    expect(screen.queryByText(/File list could not be refreshed/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete', exact: true })).toBeEnabled();
  });

  it('discloses a refresh failure after successful uploads without reporting a rejected upload', async () => {
    failRefresh = true;
    await upload([new File(['one'], 'qa1482-one.txt', { type: 'text/plain' })]);
    expect(await screen.findByText('File list could not be refreshed. File listing unavailable.')).toBeInTheDocument();
    expect(stored.map((file) => file.name)).toEqual(['qa1482-one.txt']);
    expect(postedNames).toEqual(['qa1482-one.txt']);
    expect(screen.queryByText(/Could not upload|not allowed/)).not.toBeInTheDocument();
  });

  it('does not replace a newer opening list when an old upload refresh completes', async () => {
    let finishOldRefresh;
    const oldRefresh = new Promise((resolve) => { finishOldRefresh = resolve; });
    const fetchNormally = authFetchMock.getMockImplementation();
    let listReads = 0;
    authFetchMock.mockImplementation((path, options = {}) => {
      if (path === '/api/workbench/files' && !options.method && ++listReads === 2) return oldRefresh;
      return fetchNormally(path, options);
    });
    const view = await upload([new File(['one'], 'qa1482-one.txt', { type: 'text/plain' })]);
    await waitFor(() => expect(listReads).toBe(2));
    view.rerender(<MemoryPanel isOpen={false} onClose={() => {}} />);
    stored.push({ name: 'qa1482-new-opening.txt', size: 3, mime: 'text/plain', modified_at: '2026-10-08T00:00:00Z' });
    view.rerender(<MemoryPanel isOpen onClose={() => {}} />);
    expect(await screen.findByText('qa1482-new-opening.txt')).toBeInTheDocument();
    await act(async () => { finishOldRefresh(response(200, { files: [stored[0]] })); });
    expect(screen.getByText('qa1482-new-opening.txt')).toBeInTheDocument();
  });

  it('does not replace a newer opening error when a retired upload is rejected', async () => {
    let finishRejectedPost;
    const rejectedPost = new Promise((resolve) => { finishRejectedPost = resolve; });
    const fetchNormally = authFetchMock.getMockImplementation();
    authFetchMock.mockImplementation(async (path, options = {}) => {
      if (path === '/api/workbench/files' && options.method === 'POST' && options.body.get('file').name.endsWith('.html')) {
        await rejectedPost;
      }
      return fetchNormally(path, options);
    });
    const view = await upload([
      new File(['one'], 'qa1482-one.txt', { type: 'text/plain' }),
      new File(['plain text'], 'qa1482-blocked.html', { type: 'text/html' }),
    ]);
    await waitFor(() => expect(authFetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(2));
    view.rerender(<MemoryPanel isOpen={false} onClose={() => {}} />);
    failRefresh = true;
    view.rerender(<MemoryPanel isOpen onClose={() => {}} />);
    expect(await screen.findByText('File listing unavailable.')).toBeInTheDocument();
    await act(async () => { finishRejectedPost(); });
    expect(screen.getByText('File listing unavailable.')).toBeInTheDocument();
    expect(screen.queryByText('File type .html is not allowed.')).not.toBeInTheDocument();
  });
});
