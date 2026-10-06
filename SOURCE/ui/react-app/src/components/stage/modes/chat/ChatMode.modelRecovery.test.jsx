// New reconstruction after workspace loss. Requires fresh execution and review.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../../../test/test-utils';
import ChatMode from './ChatMode';

vi.mock('../../../../hooks/useWebSocket', () => ({ useWebSocket: () => ({}) }));
const response = (data, status = 200) => new Response(JSON.stringify(status === 200
  ? { ok: true, data, error: null }
  : { ok: false, data, error: { code: 'model_save_refused', message: 'Synthetic model save refused' } }),
{ status, headers: { 'Content-Type': 'application/json' } });
// Captured from the authenticated route handler, RouteToolbox and SafeJSONResponse.
// Synthetic store and authenticated principal; no native persistence or network.
const serializedPatchCases = [
  {
    "name": "accepted",
    "request": {
      "model": "model-b"
    },
    "status": 200,
    "body": {
      "ok": true,
      "error": null,
      "data": {
        "thread": {
          "id": "thread-1",
          "title": "Synthetic conversation",
          "model": "model-b",
          "updated_at": 1778529600
        }
      }
    }
  },
  {
    "name": "cleared",
    "request": {
      "model": ""
    },
    "status": 200,
    "body": {
      "ok": true,
      "error": null,
      "data": {
        "thread": {
          "id": "thread-1",
          "title": "Synthetic conversation",
          "model": null,
          "updated_at": 1778529600
        }
      }
    }
  },
  {
    "name": "store_failure",
    "request": {
      "model": "model-b"
    },
    "status": 500,
    "body": {
      "ok": false,
      "error": {
        "code": "internal_error",
        "message": "An unexpected server error occurred.",
        "details": {
          "route": "/v1/chat/threads/{thread_id}",
          "method": "PATCH"
        }
      },
      "data": null
    }
  }
];
const deferred = () => {
  let resolve;
  const promise = new Promise(accept => { resolve = accept; });
  return { promise, resolve };
};

describe('Chat model recovery through actual apiFetch', () => {
  const thread = { id: 'thread-1', title: 'Synthetic conversation', model: 'model-a', updated_at: 1778529600 };
  let patchModel, createThread, unexpected, availableThreads, modelCatalog;
  beforeEach(() => {
    window.viola = {};
    window.__VIOLA_API_KEY__ = 'synthetic-model-recovery';
    window.history.replaceState({}, '', '/app');
    unexpected = [];
    availableThreads = [thread];
    modelCatalog = vi.fn(async () => response({ current_model: 'model-a', provider: 'Synthetic', models: ['model-a', 'model-b', 'model-c'] }));
    patchModel = vi.fn(async (_path, options) => response({ thread: { ...thread, ...JSON.parse(options.body) } }));
    createThread = vi.fn(async () => response({ thread: { ...thread, id: 'thread-2', title: 'New chat' } }));
    vi.stubGlobal('fetch', async (path, options = {}) => {
      if (path === '/v1/chat/models') return modelCatalog();
      if (path === '/v1/chat/threads/thread-1' && options.method === 'PATCH') return patchModel(path, options);
      if (path === '/v1/chat/threads' && options.method === 'POST') return createThread(path, options);
      if (path === '/v1/chat/threads' || path.startsWith('/v1/chat/threads?')) return response({ threads: availableThreads });
      if (path === '/v1/chat/threads/thread-1') return response({ thread, messages: [], active_stream_ids: [] });
      unexpected.push(path);
      throw new Error(`Unexpected synthetic request: ${path}`);
    });
  });
  afterEach(() => {
    expect(unexpected).toEqual([]);
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete window.viola;
    delete window.__VIOLA_API_KEY__;
  });
  async function mount() {
    const view = render(<ChatMode principalKey="synthetic-owner" />);
    await screen.findByDisplayValue('Synthetic conversation');
    const select = screen.getByRole('combobox', { name: 'Model' });
    await waitFor(() => expect(select).toHaveValue('model-a'));
    return { view, select };
  }
  async function choose(select, model) {
    await act(async () => fireEvent.change(select, { target: { value: model } }));
  }

  it('keeps an accepted selection and sends its exact requested model', async () => {
    const { select } = await mount();
    await choose(select, 'model-b');
    expect(JSON.parse(patchModel.mock.calls[0][1].body)).toEqual({ model: 'model-b' });
    expect(select).toHaveValue('model-b');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([400, 503])('keeps the confirmed choice and visible feedback after HTTP%s refusal', async status => {
    patchModel.mockResolvedValueOnce(response(null, status));
    const { select } = await mount();
    await choose(select, 'model-b');
    expect(select).toHaveValue('model-a');
    expect(select).toBeEnabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Could not confirm the model change');
  });

  it('shows a bounded pending request and coalesces repeated selection intent', async () => {
    const pending = deferred();
    patchModel.mockReturnValueOnce(pending.promise);
    const { select } = await mount();
    await act(async () => {
      fireEvent.change(select, { target: { value: 'model-b' } });
      fireEvent.change(select, { target: { value: 'model-c' } });
    });
    expect(patchModel).toHaveBeenCalledTimes(1);
    expect(select).toHaveValue('model-a');
    expect(select).toBeDisabled();
    expect(select).toHaveAttribute('aria-busy', 'true');
    await act(async () => pending.resolve(response({ thread: { ...thread, model: 'model-b' } })));
    expect(select).toHaveValue('model-b');
    expect(select).toBeEnabled();
  });

  it.each(['model-c', null, ''])('adopts the exact server acknowledgement %s', async model => {
    patchModel.mockResolvedValueOnce(response({ thread: { ...thread, model } }));
    const { select } = await mount();
    await choose(select, 'model-b');
    expect(select).toHaveValue(model ?? '');
  });

  it('rejects an explicit legacy refusal with a plausible thread payload', async () => {
    patchModel.mockResolvedValueOnce(new Response(JSON.stringify({ ok: false, thread: { ...thread, model: 'model-b' }, error: { code: 'synthetic_refusal' } }), { status: 200 }));
    const { select } = await mount();
    await choose(select, 'model-b');
    expect(select).toHaveValue('model-a');
    expect(screen.getByRole('alert')).toHaveTextContent('Could not confirm the model change');
  });

  it.each([
    ['wrong thread', { thread: { ...thread, id: 'other', model: 'model-b' } }],
    ['missing thread', {}],
    ['missing model', { thread: { id: thread.id } }],
    ['non-string model', { thread: { ...thread, model: false } }],
  ])('rejects a malformed %s acknowledgement', async (_name, data) => {
    patchModel.mockResolvedValueOnce(response(data));
    const { select } = await mount();
    await choose(select, 'model-b');
    expect(select).toHaveValue('model-a');
    expect(screen.getByRole('alert')).toHaveTextContent('Could not confirm the model change');
  });

  it('enforces the deadline even before the delayed timer task runs', async () => {
    const pending = deferred();
    patchModel.mockReturnValueOnce(pending.promise);
    const { select } = await mount();
    const now = vi.spyOn(performance, 'now').mockReturnValue(100);
    await choose(select, 'model-b');
    now.mockReturnValue(15100);
    await act(async () => pending.resolve(response({ thread: { ...thread, model: 'model-b' } })));
    expect(select).toHaveValue('model-a');
    expect(select).toBeEnabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Could not confirm the model change');
  });

  it('ignores a late timed-out acknowledgement after a newer successful selection', async () => {
    const pending = deferred();
    patchModel.mockReturnValueOnce(pending.promise);
    const { select } = await mount();
    vi.useFakeTimers();
    await choose(select, 'model-b');
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    await choose(select, 'model-c');
    expect(select).toHaveValue('model-c');
    await act(async () => pending.resolve(response({ thread: { ...thread, model: 'model-b' } })));
    expect(select).toHaveValue('model-c');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('retires the old model operation at new-chat intent before creation finishes', async () => {
    const model = deferred();
    const created = deferred();
    patchModel.mockReturnValueOnce(model.promise);
    createThread.mockReturnValueOnce(created.promise);
    const { select } = await mount();
    await choose(select, 'model-b');
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'New chat', exact: true })));
    expect(JSON.parse(createThread.mock.calls[0][1].body).model).toBe('model-a');
    await act(async () => model.resolve(response({ thread: { ...thread, model: 'model-b' } })));
    expect(select).toHaveValue('model-a');
    await act(async () => created.resolve(response({ thread: { ...thread, id: 'thread-2', title: 'New chat' } })));
    expect(select).toHaveValue('model-a');
  });

  it('does not revert a concurrent title acknowledgement', async () => {
    const pending = deferred();
    patchModel.mockReturnValueOnce(pending.promise);
    const { select } = await mount();
    await choose(select, 'model-b');
    const title = screen.getByRole('textbox', { name: 'Conversation title' });
    await act(async () => {
      fireEvent.change(title, { target: { value: 'Acknowledged new title' } });
      fireEvent.blur(title);
    });
    await screen.findByText('Acknowledged new title');
    await act(async () => pending.resolve(response({ thread: { ...thread, model: 'model-b' } })));
    expect(screen.getByText('Acknowledged new title')).toBeInTheDocument();
    expect(select).toHaveValue('model-b');
  });
  it('retains a local choice without PATCH when no conversation exists', async () => {
    availableThreads = [];
    render(<ChatMode principalKey="synthetic-owner" />);
    const select = screen.getByRole('combobox', { name: 'Model' });
    await waitFor(() => expect(select).toHaveValue('model-a'));
    await choose(select, 'model-b');
    expect(select).toHaveValue('model-b');
    expect(patchModel).not.toHaveBeenCalled();
  });

  it('recovers from a rejected transport promise without hiding uncertainty', async () => {
    patchModel.mockRejectedValueOnce(new TypeError('Synthetic transport unavailable'));
    const { select } = await mount();
    await choose(select, 'model-b');
    expect(select).toHaveValue('model-a');
    expect(select).toBeEnabled();
    expect(screen.getByRole('alert')).toHaveTextContent('It may have reached the server');
    await choose(select, 'model-c');
    expect(select).toHaveValue('model-c');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('retires a late failure when the principal changes even for the same thread id', async () => {
    const pending = deferred();
    patchModel.mockReturnValueOnce(pending.promise);
    const { view, select } = await mount();
    await choose(select, 'model-b');
    await act(async () => view.rerender(<ChatMode principalKey="new-synthetic-owner" />));
    await waitFor(() => expect(select).toBeEnabled());
    await waitFor(() => expect(select).toHaveValue('model-a'));
    await choose(select, 'model-c');
    expect(select).toHaveValue('model-c');
    await act(async () => pending.resolve(response(null, 503)));
    expect(select).toHaveValue('model-c');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('removes its owned deadline at unmount and ignores the late response', async () => {
    const pending = deferred();
    patchModel.mockReturnValueOnce(pending.promise);
    const { view, select } = await mount();
    vi.useFakeTimers();
    await choose(select, 'model-b');
    // Drain only JSDOM's unrelated zero-delay selectionchange task.
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => pending.resolve(response({ thread: { ...thread, model: 'model-b' } })));
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(serializedPatchCases)('handles captured server wire response $name through apiFetch', async capture => {
    patchModel.mockResolvedValueOnce(new Response(JSON.stringify(capture.body), {
      status: capture.status, headers: { 'Content-Type': 'application/json' },
    }));
    const { select } = await mount();
    await choose(select, capture.request.model);
    expect(JSON.parse(patchModel.mock.calls[0][1].body)).toEqual(capture.request);
    if (capture.status === 200) {
      expect(select).toHaveValue(capture.body.data.thread.model ?? '');
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } else {
      expect(select).toHaveValue('model-a');
      expect(screen.getByRole('alert')).toHaveTextContent('Could not confirm the model change');
    }
  });

  it.each(['normalized-server-model', null])('preserves acknowledged %s when an older focused catalog completes', async model => {
    const catalog = deferred();
    const { select } = await mount();
    modelCatalog.mockReturnValueOnce(catalog.promise);
    await act(async () => fireEvent.focus(select));
    patchModel.mockResolvedValueOnce(response({ thread: { ...thread, model } }));
    await choose(select, 'model-b');
    expect(select).toHaveValue(model ?? '');
    await act(async () => catalog.resolve(response({ current_model: 'model-a', models: ['model-a', 'model-b'] })));
    expect(select).toHaveValue(model ?? '');
    expect(select).toBeEnabled();
  });

  it('ignores an older model catalog failure after an acknowledged selection', async () => {
    const catalog = deferred();
    const { select } = await mount();
    modelCatalog.mockReturnValueOnce(catalog.promise);
    await act(async () => fireEvent.focus(select));
    await choose(select, 'model-b');
    expect(select).toHaveValue('model-b');
    await act(async () => catalog.resolve(response(null, 503)));
    expect(select).toHaveValue('model-b');
    expect(select).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['normalized-server-model', null])('retains confirmed %s through a later fresh catalog refresh', async model => {
    const { select } = await mount();
    patchModel.mockResolvedValueOnce(response({ thread: { ...thread, model } }));
    await choose(select, 'model-b');
    expect(select).toHaveValue(model ?? '');
    await act(async () => fireEvent.focus(select));
    expect(select).toHaveValue(model ?? '');
    expect(select).toBeEnabled();
    expect(screen.getByRole('option', { name: 'Synthetic - model-c' })).toBeInTheDocument();
  });

  it.each(['normalized-server-model', null])('retains confirmed %s visibly on catalog failure and Retry', async model => {
    const { select } = await mount();
    patchModel.mockResolvedValueOnce(response({ thread: { ...thread, model } }));
    await choose(select, 'model-b');
    modelCatalog.mockResolvedValueOnce(response(null, 503));
    await act(async () => fireEvent.focus(select));
    expect(select).toHaveValue(model ?? '');
    expect(select).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load the model list");
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry model list' })));
    expect(select).toHaveValue(model ?? '');
    expect(select).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('retires a confirmed model when the principal changes despite a repeated thread id', async () => {
    const { view, select } = await mount();
    patchModel.mockResolvedValueOnce(response({ thread: { ...thread, model: 'normalized-server-model' } }));
    await choose(select, 'model-b');
    expect(select).toHaveValue('normalized-server-model');
    await act(async () => view.rerender(<ChatMode principalKey="new-synthetic-owner" />));
    await waitFor(() => expect(select).toHaveValue('model-a'));
    expect(screen.queryByRole('option', { name: 'normalized-server-model' })).not.toBeInTheDocument();
  });

});
