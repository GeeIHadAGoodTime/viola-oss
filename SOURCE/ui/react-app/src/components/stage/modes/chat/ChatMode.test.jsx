import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../../../test/test-utils';
import ChatMode from './ChatMode';
import ToolUseCard from './ToolUseCard';

const chatHarness = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  buildStreamUrl: vi.fn(),
  useWebSocket: vi.fn(),
}));

vi.mock('../../../../hooks/useViolaApi', () => ({
  apiFetch: chatHarness.apiFetch,
  buildStreamUrl: chatHarness.buildStreamUrl,
}));

vi.mock('../../../../hooks/useWebSocket', () => ({
  useWebSocket: chatHarness.useWebSocket,
}));

const thread = {
  id: 'thread-1',
  title: 'Existing thread',
  updated_at: 1778529600,
};

const assistantMessage = {
  id: 'message-1',
  role: 'assistant',
  content: 'Initial response',
  status: 'complete',
  metadata: {},
};

function findRegisteredCommand(registerCommands, label) {
  for (let index = registerCommands.mock.calls.length - 1; index >= 0; index -= 1) {
    const commands = registerCommands.mock.calls[index][1] || [];
    const command = commands.find((item) => item.label === label);
    if (command) return command;
  }
  return null;
}

describe('ChatMode command registry', () => {
  afterEach(() => {
    vi.useRealTimers();
    delete window.EventSource;
    // #1064: Workbench uploads are desktop-only (featureSurface.js
    // isFeatureHidden('workbench')); restore the no-bridge default so other
    // suites in this file aren't affected by this suite's desktop signal.
    delete window.viola;
  });

  beforeEach(() => {
    vi.clearAllMocks();
    // Workbench file uploads (used by the drag-drop tests below) are
    // desktop-only per #1064 -- the Qt bridge signals "desktop app" to
    // featureSurface.js so uploadFilesToWorkbench() actually fires instead
    // of short-circuiting with the cloud-SPA upsell message.
    window.viola = {};
    chatHarness.buildStreamUrl.mockResolvedValue('/v1/chat/streams/stream-1/events');
    chatHarness.useWebSocket.mockReturnValue({});
    window.EventSource = class {
      constructor(url) {
        this.url = url;
      }

      close() {}
    };
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/models') {
        return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      }
      if (url === '/api/workbench/files' && options.method === 'POST') {
        return Promise.resolve({ name: 'notes.txt', size: 5, mime: 'text/plain' });
      }
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
        return Promise.resolve({ threads: [thread] });
      }
      if (url === '/v1/chat/threads/thread-1') {
        return Promise.resolve({ thread, messages: [assistantMessage] });
      }
      if (
        url === '/v1/chat/threads/thread-1/regenerate/message-1'
        && options.method === 'POST'
      ) {
        return Promise.resolve({
          thread,
          messages: [{ ...assistantMessage, content: '' }],
          stream_id: 'stream-1',
        });
      }
      return Promise.resolve({});
    });
  });

  it('REVIEW rejects a late new-chat POST from a retired principal', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let accept;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/threads' && options.method === 'POST') return new Promise((resolve) => { accept = resolve; });
      return fallback(url, options);
    });
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'New chat', exact: true }));
    view.rerender(<ChatMode principalKey="owner-b" />);
    await screen.findByText('Initial response');
    await act(async () => { accept({ thread: { id: 'private-old', title: 'PRIVATE OLD TITLE' } }); });
    expect(screen.queryByDisplayValue('PRIVATE OLD TITLE')).not.toBeInTheDocument();
    expect(screen.queryByText('PRIVATE OLD TITLE')).not.toBeInTheDocument();
  });

  it('REVIEW retires prior-principal model options while replacement is unresolved', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let defer = false;
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url === '/v1/chat/models' && defer) return new Promise(() => {});
      return fallback(url, options);
    });
    const view = render(<ChatMode principalKey="owner-a" />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('gpt-test'));
    defer = true;
    view.rerender(<ChatMode principalKey="owner-b" />);
    await act(async () => {});
    expect(screen.getByRole('combobox', { name: 'Model' })).not.toHaveTextContent('gpt-test');
  });

  it('REVIEW does not send a retired principal model after replacement catalog fails', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let failModels = false;
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url === '/v1/chat/models' && failModels) return Promise.reject(Object.assign(new Error('outage'), { status: 503 }));
      if (url.endsWith('/send')) return new Promise(() => {});
      return fallback(url, options);
    });
    const view = render(<ChatMode principalKey="owner-a" />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('gpt-test'));
    failModels = true;
    view.rerender(<ChatMode principalKey="owner-b" />);
    await screen.findByText("Couldn't load the model list. Please try again.");
    await screen.findByText('Initial response');
    fireEvent.change(screen.getByPlaceholderText('Message Viola'), { target: { value: 'New owner prompt' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => expect(chatHarness.apiFetch.mock.calls.some(([url]) => url.endsWith('/send'))).toBe(true));
    const [, options] = chatHarness.apiFetch.mock.calls.find(([url]) => url.endsWith('/send'));
    expect(JSON.parse(options.body).model).toBeNull();
  });

  it('REVIEW rejects a late delete snapshot from a retired principal', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    const oldOther = { ...thread, id: 'private-other', title: 'PRIVATE OTHER THREAD' };
    let nextOwner = false;
    let accept;
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (options.method === 'DELETE') return new Promise((resolve) => { accept = resolve; });
      if (url === '/v1/chat/threads') return Promise.resolve({ threads: nextOwner ? [thread] : [thread, oldOther] });
      return fallback(url, options);
    });
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('PRIVATE OTHER THREAD');
    fireEvent.click(screen.getAllByRole('button', { name: 'Conversation menu' })[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Delete', exact: true }));
    nextOwner = true;
    view.rerender(<ChatMode principalKey="owner-b" />);
    await screen.findByText('Initial response');
    await act(async () => { accept({ deleted: true }); });
    expect(screen.queryByText('PRIVATE OTHER THREAD')).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it('REVIEW retires prior-principal title and unsent draft for an empty next workspace', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let nextOwner = false;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/threads' && nextOwner) return Promise.resolve({ threads: [] });
      return fallback(url, options);
    });
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    fireEvent.change(screen.getByPlaceholderText('Message Viola'), { target: { value: 'PRIVATE UNSENT DRAFT' } });
    fireEvent.change(screen.getByLabelText('Search chats'), { target: { value: 'PRIVATE SEARCH' } });
    nextOwner = true;
    view.rerender(<ChatMode principalKey="owner-b" />);
    await waitFor(() => expect(screen.queryByText('Loading chats...')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Conversation title')).not.toHaveValue('Existing thread');
    expect(screen.getByPlaceholderText('Message Viola')).toHaveValue('');
    expect(screen.getByLabelText('Search chats')).toHaveValue('');
  });

  it('REVIEW rejects a retired-principal upload result and does not dispatch the remaining files', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let accept;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => url === '/api/workbench/files'
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    const files = [new File(['one'], 'private-one.txt'), new File(['two'], 'private-two.txt')];
    fireEvent.drop(document.querySelector('.chat-mode'), { dataTransfer: { files, types: ['Files'] } });
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url === '/api/workbench/files')).toHaveLength(1);
    view.rerender(<ChatMode principalKey="owner-b" />);
    await screen.findByText('Initial response');
    await act(async () => { accept({ name: 'PRIVATE-UPLOAD.txt' }); });
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url === '/api/workbench/files')).toHaveLength(1);
    expect(screen.getByPlaceholderText('Message Viola')).not.toHaveValue(expect.stringContaining('PRIVATE-UPLOAD'));
    expect(screen.queryByText(/Uploaded PRIVATE-UPLOAD/)).not.toBeInTheDocument();
  });

  it('REVIEW rejects a retired-principal model update response', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let accept;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => options.method === 'PATCH'
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), { target: { value: '' } });
    view.rerender(<ChatMode principalKey="owner-b" />);
    await screen.findByText('Initial response');
    await act(async () => { accept({ thread: { ...thread, title: 'PRIVATE MODEL RESPONSE' } }); });
    expect(screen.queryByText('PRIVATE MODEL RESPONSE')).not.toBeInTheDocument();
  });

  it('REVIEW ignores old feedback content after a principal switch', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let accept;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => url.endsWith('/feedback')
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByLabelText('Thumbs up'));
    view.rerender(<ChatMode principalKey="owner-b" />);
    await screen.findByText('Initial response');
    await act(async () => { accept({ message: { ...assistantMessage, content: 'PRIVATE OLD FEEDBACK' } }); });
    expect(screen.queryByText('PRIVATE OLD FEEDBACK')).not.toBeInTheDocument();
  });

  it('REVIEW preserves an intentional same-principal model choice through a catalog outage and retry', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let fail = false;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/models') return fail ? Promise.reject(new Error('503 outage'))
        : Promise.resolve({ current_model: 'gpt-test', provider: 'test', models: ['gpt-test', 'chosen-model'] });
      if (options.method === 'PATCH') return Promise.resolve({ thread: { ...thread, model: JSON.parse(options.body).model } });
      return fallback(url, options);
    });
    render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    const selector = screen.getByRole('combobox', { name: 'Model' });
    await act(async () => { fireEvent.change(selector, { target: { value: 'chosen-model' } }); });
    fail = true;
    fireEvent.focus(selector);
    await screen.findByText("Couldn't load the model list. Please try again.");
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry model list' }));
    await waitFor(() => expect(selector).toHaveValue('chosen-model'));
    expect(chatHarness.apiFetch.mock.calls.filter(([, options]) => options?.method === 'PATCH')).toHaveLength(1);
  });

  it('REVIEW keeps same-principal new-chat creation functional without extra requests', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options = {}) => url === '/v1/chat/threads' && options.method === 'POST'
      ? Promise.resolve({ thread: { id: 'new-current', title: 'New current chat' } }) : fallback(url, options));
    render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'New chat', exact: true }));
    await screen.findByDisplayValue('New current chat');
    expect(chatHarness.apiFetch.mock.calls.filter(([url, options]) => url === '/v1/chat/threads' && options?.method === 'POST')).toHaveLength(1);
  });

  it('shows a model-catalog outage and recovers through an explicit retry', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let available = false;
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url === '/v1/chat/models' && !available) return Promise.reject(new Error('503 unavailable'));
      return fallback(url, options);
    });
    render(<ChatMode />);
    await screen.findByText("Couldn't load the model list. Please try again.");
    const selector = screen.getByRole('combobox', { name: 'Model' });
    expect(selector).toBeDisabled();
    expect(selector).toHaveTextContent('Model list unavailable');
    expect(selector).not.toHaveTextContent('Default model');
    available = true;
    fireEvent.click(screen.getByRole('button', { name: 'Retry model list' }));
    await waitFor(() => expect(selector).toBeEnabled());
    expect(selector).toHaveValue('gpt-test');
    expect(screen.queryByText("Couldn't load the model list. Please try again.")).not.toBeInTheDocument();
  });

  it('hides an obsolete model catalog after a refresh failure until the real catalog returns', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let unavailable = false;
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url === '/v1/chat/models' && unavailable) return Promise.reject(new Error('503 unavailable'));
      return fallback(url, options);
    });
    render(<ChatMode />);
    const selector = screen.getByRole('combobox', { name: 'Model' });
    await waitFor(() => expect(selector).toHaveValue('gpt-test'));
    unavailable = true;
    fireEvent.focus(selector);
    await screen.findByText("Couldn't load the model list. Please try again.");
    expect(selector).toBeDisabled();
    expect(selector).not.toHaveTextContent('gpt-test');
    chatHarness.apiFetch.mockImplementation((url, options) => url === '/v1/chat/models'
      ? Promise.resolve({ current_model: 'local-fixture', provider: 'ollama', models: ['local-fixture'] })
      : fallback(url, options));
    fireEvent.click(screen.getByRole('button', { name: 'Retry model list' }));
    await waitFor(() => expect(selector).toHaveValue('local-fixture'));
    expect(selector).toHaveTextContent('ollama - local-fixture');
    expect(selector).not.toHaveTextContent('gpt-test');
  });

  it('does not let an older model refresh failure replace a newer successful catalog', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    const pending = [];
    let deferModels = false;
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url === '/v1/chat/models' && deferModels) return new Promise((resolve, reject) => pending.push({ resolve, reject }));
      return fallback(url, options);
    });
    render(<ChatMode />);
    const selector = screen.getByRole('combobox', { name: 'Model' });
    await waitFor(() => expect(selector).toHaveValue('gpt-test'));
    deferModels = true;
    fireEvent.focus(selector);
    fireEvent.focus(selector);
    expect(pending).toHaveLength(2);
    await act(async () => { pending[1].resolve({ current_model: 'new-model', provider: 'local', models: ['new-model'] }); });
    await waitFor(() => expect(selector).toHaveValue('new-model'));
    await act(async () => { pending[0].reject(new Error('older failure')); });
    expect(selector).toBeEnabled();
    expect(selector).toHaveValue('new-model');
    expect(screen.queryByText("Couldn't load the model list. Please try again.")).not.toBeInTheDocument();
  });

  it('shows export failure and allows retry without duplicate pending requests', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let rejectExport;
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url.endsWith('/export')) return new Promise((resolve, reject) => { rejectExport = reject; });
      return fallback(url, options);
    });
    render(<ChatMode />);
    await screen.findByDisplayValue('Existing thread');
    const button = screen.getByRole('button', {name:'Export',exact:true});
    fireEvent.click(button);
    fireEvent.click(button);
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.endsWith('/export'))).toHaveLength(1);
    await act(async () => rejectExport(new Error('synthetic offline')));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not export this chat');
    expect(screen.getByRole('button',{name:'Export',exact:true})).toBeEnabled();
  });

  it('rejects an overlong rename with visible feedback and no request', async () => {
    render(<ChatMode />);
    const title = await screen.findByDisplayValue('Existing thread');
    fireEvent.change(title, { target: { value: 'x'.repeat(201) } });
    fireEvent.blur(title);
    expect(await screen.findByRole('alert')).toHaveTextContent('200 characters or fewer');
    expect(chatHarness.apiFetch.mock.calls.filter(([, opts]) => opts?.method === 'PATCH')).toHaveLength(0);
    expect(title).toHaveValue('x'.repeat(201));
  });

  it('shows a failed rename and clears the error after a successful retry', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    let fail = true;
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (options.method === 'PATCH') {
        if (fail) return Promise.reject(new Error('synthetic offline'));
        return Promise.resolve({ thread: { ...thread, title: JSON.parse(options.body).title } });
      }
      return fallback(url, options);
    });
    render(<ChatMode />);
    const title = await screen.findByDisplayValue('Existing thread');
    fireEvent.change(title, { target: { value: 'Renamed chat' } });
    fireEvent.blur(title);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not confirm the new chat title');
    expect(title).toHaveValue('Renamed chat');
    fail = false;
    fireEvent.blur(title);
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText('Existing thread')).not.toBeInTheDocument());
  });

  it('counts Unicode characters consistently with the server title limit', async () => {
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options = {}) => options.method === 'PATCH'
      ? Promise.resolve({ thread: { ...thread, title: JSON.parse(options.body).title } })
      : fallback(url, options));
    render(<ChatMode />);
    const title = await screen.findByDisplayValue('Existing thread');
    fireEvent.change(title, { target: { value: '🎵'.repeat(200) } });
    fireEvent.blur(title);
    await waitFor(() => expect(chatHarness.apiFetch).toHaveBeenCalledWith(
      '/v1/chat/threads/thread-1', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ title: '🎵'.repeat(200) }) })
    ));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('registers active chat commands and reruns the last assistant response', async () => {
    const registerCommands = vi.fn(() => vi.fn());

    render(
      <ChatMode
        commandScopeActive
        commandRegistry={{ registerCommands }}
        profileName="Jay"
      />
    );

    await waitFor(() => {
      expect(findRegisteredCommand(registerCommands, 'Regenerate last message')).toBeTruthy();
    });

    const regenerate = findRegisteredCommand(registerCommands, 'Regenerate last message');
    expect(findRegisteredCommand(registerCommands, 'Export current thread')).toBeTruthy();
    expect(findRegisteredCommand(registerCommands, 'Hide chat list')).toBeTruthy();

    act(() => {
      regenerate.perform();
    });

    await waitFor(() => {
      expect(chatHarness.apiFetch).toHaveBeenCalledWith(
        '/v1/chat/threads/thread-1/regenerate/message-1',
        expect.objectContaining({ method: 'POST' })
      );
    });
  });

  it('uploads dropped files to Workbench and references them in the draft', async () => {
    render(<ChatMode profileName="Jay" />);

    const chat = await screen.findByTestId('chat-mode');
    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' });
    const dataTransfer = {
      files: [file],
      types: ['Files'],
      dropEffect: 'copy',
    };

    fireEvent.dragEnter(chat, { dataTransfer });
    expect(screen.getByTestId('chat-file-drop-overlay')).toBeInTheDocument();
    fireEvent.drop(chat, { dataTransfer });

    await waitFor(() => {
      expect(chatHarness.apiFetch).toHaveBeenCalledWith(
        '/api/workbench/files',
        expect.objectContaining({
          method: 'POST',
          body: expect.any(FormData),
        })
      );
    });
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Message Viola')).toHaveValue('Workbench file: notes.txt');
    });
    expect(screen.getByText('Uploaded notes.txt to Workbench.')).toBeInTheDocument();
  });

  it('#1064: on the cloud SPA, drops a file without calling the dead /api/workbench/files fetch and shows an honest message', async () => {
    // No window.viola bridge -> cloud SPA surface (see afterEach for the
    // desktop-mode default this suite otherwise runs under).
    delete window.viola;

    render(<ChatMode profileName="Jay" />);

    const chat = await screen.findByTestId('chat-mode');
    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' });
    const dataTransfer = {
      files: [file],
      types: ['Files'],
      dropEffect: 'copy',
    };

    fireEvent.dragEnter(chat, { dataTransfer });
    fireEvent.drop(chat, { dataTransfer });

    await waitFor(() => {
      expect(screen.getByText('File attachments are available in the desktop app.')).toBeInTheDocument();
    });
    expect(chatHarness.apiFetch).not.toHaveBeenCalledWith('/api/workbench/files', expect.anything());
  });

  it('logs to console instead of silently dropping a failed dropped-file upload (LEVERAGE_RANKING quick-kill #4, F4)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(<ChatMode profileName="Jay" />);

    const chat = await screen.findByTestId('chat-mode');
    // uploadFilesToWorkbench() already catches its own per-file upload errors
    // internally (sets uploadError, never rejects), so the ONLY way handleDrop's
    // `.catch()` at ChatMode.jsx:322 actually fires is if something throws
    // *before* that internal try/catch - e.g. a malformed FileList that throws
    // while being converted with Array.from(). This proves that when that does
    // happen, the failure is now logged instead of silently dropped.
    const throwingFiles = {
      length: 1,
      0: undefined,
      [Symbol.iterator]() {
        throw new Error('simulated malformed FileList');
      },
    };
    const dataTransfer = {
      files: throwingFiles,
      types: ['Files'],
      dropEffect: 'copy',
    };

    fireEvent.dragEnter(chat, { dataTransfer });
    fireEvent.drop(chat, { dataTransfer });

    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        '[ChatMode] Drag-drop file upload failed:',
        expect.any(Error)
      );
    });

    consoleErrorSpy.mockRestore();
  });

  it('#2395: switching principalKey (a sign-in that changes the workspace identity) drops the stale thread list and refetches', async () => {
    let identity = 'device';
    const deviceThread = { id: 'thread-device', title: 'Device chat', updated_at: 1778529600 };
    const deviceMessage = {
      id: 'msg-device', role: 'assistant', content: 'Device-era reply', status: 'complete', metadata: {},
    };
    const userThread = { id: 'thread-user-a', title: 'User A chat', updated_at: 1778529700 };
    const userMessage = {
      id: 'msg-user-a', role: 'assistant', content: 'User A reply', status: 'complete', metadata: {},
    };

    chatHarness.apiFetch.mockImplementation((url) => {
      if (url === '/v1/chat/models') {
        return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      }
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
        return Promise.resolve({ threads: [identity === 'device' ? deviceThread : userThread] });
      }
      if (url === '/v1/chat/threads/thread-device') {
        return Promise.resolve({ thread: deviceThread, messages: [deviceMessage] });
      }
      if (url === '/v1/chat/threads/thread-user-a') {
        return Promise.resolve({ thread: userThread, messages: [userMessage] });
      }
      return Promise.resolve({});
    });

    const { rerender } = render(<ChatMode profileName="Jay" principalKey="device" />);

    await screen.findByText('Device-era reply');

    // Simulate the sign-in that switches the workspace identity -- the
    // parent (SmartDisplay) re-renders ChatMode with a new principalKey once
    // the account changes, without ever unmounting it.
    identity = 'user-a';
    rerender(<ChatMode profileName="Jay" principalKey="user-a" />);

    await screen.findByText('User A reply');
    expect(screen.queryByText('Device-era reply')).not.toBeInTheDocument();
  });

  it('C-071: a slow response from the OLD principal cannot land after a sign-out/sign-in switch and overwrite the NEW principal\'s state (cross-account bleed)', async () => {
    // The #2395 test above covers the happy path where the old identity's
    // fetch resolves BEFORE the switch. This pins the interleaved case: user
    // A's initial /v1/chat/threads request is still in flight (server is
    // slow, or the tab was backgrounded) when the sign-out/sign-in to user B
    // happens. If the boot effect's fetch helpers apply whatever they're
    // holding as soon as it resolves -- with no check for whether their
    // request is still the current principal's -- user A's stale thread list
    // silently clobbers user B's freshly-rendered one once it finally lands.
    const userAThread = { id: 'thread-user-a', title: 'User A private chat', updated_at: 1778529700 };
    const userAMessage = {
      id: 'msg-user-a', role: 'assistant', content: 'User A reply', status: 'complete', metadata: {},
    };
    const userBThread = { id: 'thread-user-b', title: 'User B private chat', updated_at: 1778529800 };
    const userBMessage = {
      id: 'msg-user-b', role: 'assistant', content: 'User B reply', status: 'complete', metadata: {},
    };

    let resolveUserAThreads = null;
    let threadsCallCount = 0;

    chatHarness.apiFetch.mockImplementation((url) => {
      if (url === '/v1/chat/models') {
        return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      }
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
        threadsCallCount += 1;
        if (threadsCallCount === 1) {
          // User A's own thread-list fetch: held open deliberately to
          // simulate a slow response that outlives the account switch.
          return new Promise((resolve) => {
            resolveUserAThreads = resolve;
          });
        }
        // User B's own thread-list fetch resolves immediately.
        return Promise.resolve({ threads: [userBThread] });
      }
      if (url === '/v1/chat/threads/thread-user-a') {
        return Promise.resolve({ thread: userAThread, messages: [userAMessage] });
      }
      if (url === '/v1/chat/threads/thread-user-b') {
        return Promise.resolve({ thread: userBThread, messages: [userBMessage] });
      }
      return Promise.resolve({});
    });

    const { rerender } = render(<ChatMode profileName="Jay" principalKey="user-a" />);

    // Let user A's boot effect fire and issue its (now-pending) thread-list
    // request.
    await waitFor(() => expect(threadsCallCount).toBe(1));

    // The sign-out/sign-in switch: SmartDisplay re-renders ChatMode with the
    // new principal while user A's request is still unresolved.
    rerender(<ChatMode profileName="Jay" principalKey="user-b" />);

    await screen.findByText('User B reply');
    expect(screen.getByText('User B private chat')).toBeInTheDocument();

    // Now user A's stale response finally lands.
    await act(async () => {
      resolveUserAThreads({ threads: [userAThread] });
      // Flush the microtask chain inside the resolved loadThreads() promise.
      await Promise.resolve();
      await Promise.resolve();
    });

    // User B's state must still be exactly what's rendered -- user A's
    // thread must never appear in user B's sidebar or thread view.
    expect(screen.getByText('User B private chat')).toBeInTheDocument();
    expect(screen.getByText('User B reply')).toBeInTheDocument();
    expect(screen.queryByText('User A private chat')).not.toBeInTheDocument();
    expect(screen.queryByText('User A reply')).not.toBeInTheDocument();
  });

  it('C-071: a slow thread-DETAIL response from the OLD principal cannot land after a sign-out/sign-in switch and render the OLD principal\'s message bodies in the NEW principal\'s pane', async () => {
    // The test above pins the thread-LIST leg (`loadThreads`). This pins the
    // thread-DETAIL leg (`loadThread`), which carries the actual message
    // bodies -- the highest-value data path of the three boot fetches, and
    // the one round-1 audit found with zero behavioral coverage. Boot
    // auto-selects the first thread and calls `loadThread(threadList[0].id)`;
    // user A's own detail fetch for their thread is still in flight when the
    // sign-out/sign-in to user B happens. If `loadThread` applies whatever
    // it's holding the moment it resolves -- with no check for whether its
    // request is still the current principal's -- user A's message content
    // silently renders in user B's chat pane.
    const userAThread = { id: 'thread-user-a', title: 'User A private chat', updated_at: 1778529700 };
    const userASecretMessage = {
      id: 'msg-user-a', role: 'assistant', content: 'SECRET-A: user A bank balance is 12345', status: 'complete', metadata: {},
    };
    const userBThread = { id: 'thread-user-b', title: 'User B private chat', updated_at: 1778529800 };
    const userBMessage = {
      id: 'msg-user-b', role: 'assistant', content: 'User B reply', status: 'complete', metadata: {},
    };

    let resolveUserADetail = null;
    let listCallCount = 0;

    chatHarness.apiFetch.mockImplementation((url) => {
      if (url === '/v1/chat/models') {
        return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      }
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
        // Isolate the DETAIL leg: both principals' thread-LIST fetches
        // resolve immediately, ordered by boot sequence (A first, then B).
        listCallCount += 1;
        return Promise.resolve({ threads: [listCallCount === 1 ? userAThread : userBThread] });
      }
      if (url === '/v1/chat/threads/thread-user-a') {
        // User A's own thread-detail fetch: held open deliberately to
        // simulate a slow response that outlives the account switch.
        return new Promise((resolve) => {
          resolveUserADetail = resolve;
        });
      }
      if (url === '/v1/chat/threads/thread-user-b') {
        return Promise.resolve({ thread: userBThread, messages: [userBMessage] });
      }
      return Promise.resolve({});
    });

    const { rerender } = render(<ChatMode profileName="Jay" principalKey="user-a" />);

    // Let user A's boot resolve its thread list (auto-selecting the thread)
    // and issue the now-pending thread-detail request.
    await waitFor(() => expect(resolveUserADetail).not.toBeNull());

    // The sign-out/sign-in switch: SmartDisplay re-renders ChatMode with the
    // new principal while user A's detail request is still unresolved.
    rerender(<ChatMode profileName="Jay" principalKey="user-b" />);

    await screen.findByText('User B reply');
    expect(screen.queryByText(userASecretMessage.content)).not.toBeInTheDocument();

    // Now user A's stale detail response finally lands.
    await act(async () => {
      resolveUserADetail({ thread: userAThread, messages: [userASecretMessage] });
      // Flush the microtask chain inside the resolved loadThread() promise.
      await Promise.resolve();
      await Promise.resolve();
    });

    // User B's message must still be exactly what's rendered -- user A's
    // message body must never appear in user B's chat pane.
    expect(screen.getByText('User B reply')).toBeInTheDocument();
    expect(screen.queryByText(userASecretMessage.content)).not.toBeInTheDocument();
  });

  it('#2395: recovers from sending into a stale (post-account-switch) thread id by starting a fresh thread', async () => {
    const staleThread = { id: 'thread-1', title: 'Existing thread', updated_at: 1778529600 };
    const freshThread = { id: 'thread-2', title: 'New chat', updated_at: 1778529700 };

    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/models') {
        return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      }
      if (url === '/v1/chat/threads' && options.method === 'POST') {
        return Promise.resolve({ thread: freshThread });
      }
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
        return Promise.resolve({ threads: [staleThread] });
      }
      if (url === '/v1/chat/threads/thread-1') {
        return Promise.resolve({ thread: staleThread, messages: [] });
      }
      if (url === '/v1/chat/threads/thread-1/send' && options.method === 'POST') {
        // Mirrors the live 2026-07-17 failure: the sidebar still held a
        // thread id minted under the previous principal, so the server
        // correctly 404s it once the session's identity has moved on.
        const err = new Error("We couldn't complete that request. Please try again.");
        err.status = 404;
        err.code = 'chat_thread_not_found';
        return Promise.reject(err);
      }
      if (url === '/v1/chat/threads/thread-2/send' && options.method === 'POST') {
        return Promise.resolve({ stream_id: 'stream-1' });
      }
      return Promise.resolve({});
    });

    render(<ChatMode profileName="Jay" />);

    const input = await screen.findByPlaceholderText('Message Viola');
    fireEvent.change(input, { target: { value: 'hello after an account switch' } });
    fireEvent.click(screen.getByLabelText('Send message'));

    await waitFor(() => {
      expect(chatHarness.apiFetch).toHaveBeenCalledWith(
        '/v1/chat/threads/thread-2/send',
        expect.objectContaining({ method: 'POST' })
      );
    });

    expect(screen.queryByText('Something went wrong while sending that message.')).not.toBeInTheDocument();
  });

  it('logs to console instead of silently dropping a failed stream-cancel request (LEVERAGE_RANKING quick-kill #4, F4)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/models') {
        return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      }
      if (url === '/v1/chat/threads' && options.method === 'POST') {
        return Promise.resolve({ thread });
      }
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
        return Promise.resolve({ threads: [] });
      }
      if (url === '/v1/chat/threads/thread-1/send' && options.method === 'POST') {
        return Promise.resolve({ stream_id: 'stream-1' });
      }
      if (url === '/v1/chat/streams/stream-1/cancel' && options.method === 'POST') {
        return Promise.reject(new Error('simulated cancel endpoint failure'));
      }
      if (url === '/v1/chat/threads/thread-1') {
        return Promise.resolve({ thread, messages: [] });
      }
      return Promise.resolve({});
    });

    render(<ChatMode profileName="Jay" />);

    const input = await screen.findByPlaceholderText('Message Viola');
    fireEvent.change(input, { target: { value: 'hello viola' } });
    fireEvent.click(screen.getByLabelText('Send message'));

    const stopButton = await screen.findByLabelText('Stop response');
    fireEvent.click(stopButton);

    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        '[ChatMode] Stream cancel request failed; stream may keep running server-side:',
        expect.any(Error)
      );
    });

    consoleErrorSpy.mockRestore();
  });
  it('keeps a stream transport failure visible while the server has only the user message', async () => {
    let source;
    window.EventSource = class {
      constructor() { source = this; }
      close = vi.fn();
    };
    const userMessage = { id: 'user-message', role: 'user', content: '2+2', status: 'complete', metadata: {} };
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/models') return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      if (url === '/v1/chat/threads/thread-1/send' && options.method === 'POST') {
        return Promise.resolve({ thread, messages: [userMessage], stream_id: 'stream-1' });
      }
      if (url === '/v1/chat/threads/thread-1') return Promise.resolve({ thread, messages: [userMessage] });
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) return Promise.resolve({ threads: [thread] });
      return Promise.resolve({});
    });
    render(<ChatMode profileName="Test user" />);
    const input = await screen.findByPlaceholderText('Message Viola');
    await screen.findByText('2+2');
    fireEvent.change(input, { target: { value: '2+2' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => expect(source?.onerror).toBeTypeOf('function'));
    await act(async () => { source.onerror(); });
    await waitFor(() => expect(screen.getByText(/The live response connection dropped\./g)).toBeInTheDocument());
    // Flush the immediate async refresh that previously erased the transient failure.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText(/The live response connection dropped\./g)).toBeInTheDocument();
    expect(source.close).toHaveBeenCalled();
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
  });

  it.each(['transport', 'send'])('preserves the first turn in a newly created thread when %s fails', async (failureKind) => {
    let source;
    window.EventSource = class {
      constructor() { source = this; }
      close = vi.fn();
    };
    const newThread = { id: 'fresh-thread', title: 'New chat' };
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/models') return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
      if (url === '/v1/chat/threads' && options.method === 'POST') return Promise.resolve({ thread: newThread });
      if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) return Promise.resolve({ threads: [] });
      if (url === '/v1/chat/threads/fresh-thread/send') {
        return failureKind === 'send'
          ? Promise.reject(Object.assign(new Error('send refused'), { status: 400 }))
          : Promise.resolve({ stream_id: 'fresh-stream' });
      }
      return Promise.resolve({});
    });
    render(<ChatMode profileName="Test user" />);
    const input = await screen.findByPlaceholderText('Message Viola');
    await waitFor(() => expect(input).not.toBeDisabled());
    fireEvent.change(input, { target: { value: 'first message' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    if (failureKind === 'transport') {
      await waitFor(() => expect(source?.onerror).toBeTypeOf('function'));
      await act(async () => { source.onerror(); });
    }
    await screen.findByText(failureKind === 'transport'
      ? /The live response connection dropped\./
      : 'Something went wrong while sending that message.');
    expect(screen.getByText('first message')).toBeInTheDocument();
    if (failureKind === 'transport') expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    else expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
  });

  function recoveryHarness() {
    const sources = [];
    window.EventSource = class {
      constructor() { sources.push(this); }
      close = vi.fn();
    };
    const state = {
      active: [],
      messages: [assistantMessage],
      unavailable: false,
      cancel: async () => ({ cancelled: true }),
      probe: null,
    };
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options = {}) => {
      if (url === '/v1/chat/streams/stream-1/cancel') return state.cancel(options);
      if (url.includes('/regenerate/')) state.active = ['stream-1'];
      if (url === '/v1/chat/threads/thread-1') {
        if (options.signal && state.probe) return state.probe(options);
        if (options.signal && state.unavailable) return Promise.reject(new Error('offline'));
        return Promise.resolve({ thread, messages: state.messages, active_stream_ids: [...state.active] });
      }
      return fallback(url, options);
    });
    return { state, sources };
  }

  async function startRecoveryTurn() {
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true }));
    await waitFor(() => expect(chatHarness.buildStreamUrl).toHaveBeenCalled());
    await act(async () => {});
  }

  it('retains Stop through a simulated 45-second producer delay after SSE loss and recovers the persisted result', async () => {
    const { state, sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    vi.useFakeTimers();
    await act(async () => { sources[0].onerror(); });
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(screen.getByText(/response is still running/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(45000); });
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(sources).toHaveLength(1); // Polling never resubmits generation or duplicates replayed tokens.
    state.active = [];
    state.messages = [{ ...assistantMessage, content: 'Recovered result', metadata: { stream_id: 'stream-1' } }];
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText('Recovered result')).toBeInTheDocument();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
    expect(screen.queryByText(/response is still running/)).not.toBeInTheDocument();
  });

  it('bounds failed status checks, shows unknown without a cursor, and retries without resending generation', async () => {
    const { state, sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    state.unavailable = true;
    vi.useFakeTimers();
    await act(async () => { sources[0].onerror(); await vi.advanceTimersByTimeAsync(4000); });
    expect(screen.getByText(/Cannot confirm the response status/)).toBeInTheDocument();
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(document.querySelector('.chat-cursor')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Regenerate', exact: true })).toBeDisabled();
    const probes = () => chatHarness.apiFetch.mock.calls.filter(([url, options]) => url === '/v1/chat/threads/thread-1' && options?.signal);
    expect(probes()).toHaveLength(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(probes()).toHaveLength(3);
    state.unavailable = false;
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry response status' })); });
    expect(screen.getByText(/response is still running/)).toBeInTheDocument();
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.includes('/regenerate/'))).toHaveLength(1);
  });

  it('bounds hanging status requests and aborts them instead of waiting forever', async () => {
    const { state, sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    const signals = [];
    state.probe = ({ signal }) => { signals.push(signal); return new Promise(() => {}); };
    vi.useFakeTimers();
    await act(async () => { sources[0].onerror(); await vi.advanceTimersByTimeAsync(19000); });
    expect(signals).toHaveLength(3);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(screen.getByText(/Cannot confirm the response status/)).toBeInTheDocument();
  });

  it('keeps Stop until cancellation is actually terminal and allows retry after a failed cancel', async () => {
    const { state, sources } = recoveryHarness();
    const log = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<ChatMode />);
    await startRecoveryTurn();
    vi.useFakeTimers();
    await act(async () => { sources[0].onerror(); });
    state.cancel = async () => { throw new Error('offline'); };
    await act(async () => { fireEvent.click(screen.getByLabelText('Stop response')); });
    expect(screen.getByText(/Could not confirm Stop/)).toBeInTheDocument();
    expect(screen.queryByText('Stopped.')).not.toBeInTheDocument();
    state.cancel = async () => ({ cancelled: true });
    await act(async () => { fireEvent.click(screen.getByLabelText('Stop response')); });
    expect(screen.getByText(/Waiting for the response to finish stopping/)).toBeInTheDocument();
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    state.active = [];
    state.messages = [{ ...assistantMessage, content: 'Stopped.', status: 'stopped', metadata: { stream_id: 'stream-1' } }];
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText('Stopped.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
    log.mockRestore();
  });

  it('can Stop while a recovery probe is unresolved and rejects its late snapshot', async () => {
    const { state, sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    let resolveProbe;
    state.probe = () => new Promise((resolve) => { resolveProbe = resolve; });
    await act(async () => { sources[0].onerror(); });
    state.probe = null;
    state.cancel = async () => {
      state.active = [];
      state.messages = [{ ...assistantMessage, content: 'Stopped.', status: 'stopped', metadata: { stream_id: 'stream-1' } }];
      return { cancelled: true };
    };
    await act(async () => { fireEvent.click(screen.getByLabelText('Stop response')); });
    expect(screen.getByText('Stopped.')).toBeInTheDocument();
    await act(async () => { resolveProbe({ thread, active_stream_ids: ['stream-1'], messages: [] }); });
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
  });

  it('queues Stop before the generation POST returns and cancels the returned stream without opening SSE', async () => {
    const { state, sources } = recoveryHarness();
    let accept;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => url.includes('/regenerate/')
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    render(<ChatMode />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true }));
    fireEvent.click(screen.getByLabelText('Stop response'));
    state.cancel = async () => {
      state.active = [];
      state.messages = [{ ...assistantMessage, status: 'stopped', content: 'Stopped.', metadata: { stream_id: 'stream-1' } }];
      return { cancelled: true };
    };
    await act(async () => { accept({ thread, messages: [{ ...assistantMessage, content: '' }], stream_id: 'stream-1' }); });
    expect(screen.getByText('Stopped.')).toBeInTheDocument();
    expect(sources).toHaveLength(0);
    expect(chatHarness.apiFetch).toHaveBeenCalledWith('/v1/chat/streams/stream-1/cancel', expect.objectContaining({ method: 'POST' }));
  });

  it.each(['switch', 'unmount'])('disposes recovery on principal %s and ignores old callbacks', async (kind) => {
    const { state, sources } = recoveryHarness();
    const view = render(<ChatMode principalKey="owner-a" />);
    await startRecoveryTurn();
    let resolveProbe;
    let signal;
    state.probe = (options) => { signal = options.signal; return new Promise((resolve) => { resolveProbe = resolve; }); };
    await act(async () => { sources[0].onerror(); });
    if (kind === 'switch') { state.active = []; view.rerender(<ChatMode principalKey="owner-b" />); }
    else view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolveProbe({ thread, active_stream_ids: [], messages: [{ ...assistantMessage, content: 'PRIVATE OLD RESULT', metadata: { stream_id: 'stream-1' } }] });
      sources[0].onmessage({ data: JSON.stringify({ done: true, content: 'PRIVATE OLD EVENT' }) });
    });
    expect(screen.queryByText('PRIVATE OLD RESULT')).not.toBeInTheDocument();
    expect(screen.queryByText('PRIVATE OLD EVENT')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
  });

  it('does not attach a stream returned by a prior principal after the generation POST was delayed', async () => {
    const { sources } = recoveryHarness();
    let accept;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => url.includes('/regenerate/')
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    const view = render(<ChatMode principalKey="owner-a" />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true }));
    view.rerender(<ChatMode principalKey="owner-b" />);
    await act(async () => { accept({ thread, messages: [{ ...assistantMessage, content: 'PRIVATE OLD POST' }], stream_id: 'stream-1' }); });
    expect(sources).toHaveLength(0);
    expect(screen.queryByText('PRIVATE OLD POST')).not.toBeInTheDocument();
  });

  it('treats an SSE connection timeout as transport loss rather than server completion', async () => {
    const { sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    await act(async () => { sources[0].onmessage({ data: JSON.stringify({ error: true, message: 'Stream timeout' }) }); });
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(screen.getByText(/response is still running/)).toBeInTheDocument();
  });

  it('recovers when stream authentication fails after the server accepted generation', async () => {
    recoveryHarness();
    chatHarness.buildStreamUrl.mockRejectedValueOnce(new Error('auth transport offline'));
    render(<ChatMode />);
    await startRecoveryTurn();
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(screen.getByText(/response is still running/)).toBeInTheDocument();
  });

  it('clears pending on a genuine terminal event and leaves no recovery polling', async () => {
    const { sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    await act(async () => { sources[0].onmessage({ data: JSON.stringify({ done: true, content: 'Terminal result' }) }); });
    expect(screen.getByText('Terminal result')).toBeInTheDocument();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
    expect(sources[0].close).toHaveBeenCalled();
    expect(chatHarness.apiFetch.mock.calls.filter(([, options]) => options?.signal)).toHaveLength(0);
  });

  it('restores Stop after a same-principal remount and replays into a clean buffer', async () => {
    const { state, sources } = recoveryHarness();
    const first = render(<ChatMode principalKey="owner-a" />);
    await startRecoveryTurn();
    await act(async () => { sources[0].onmessage({ data: JSON.stringify({ token: 'partial' }) }); });
    expect(screen.getByText('partial')).toBeInTheDocument();
    first.unmount();
    state.messages = [{ ...assistantMessage, content: '', status: 'streaming', metadata: { stream_id: 'stream-1' } }];
    render(<ChatMode principalKey="owner-a" />);
    await waitFor(() => expect(sources).toHaveLength(2));
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    await act(async () => { sources[1].onmessage({ data: JSON.stringify({ token: 'partial' }) }); });
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.queryByText('partialpartial')).not.toBeInTheDocument();
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.includes('/regenerate/'))).toHaveLength(1);
  });

  it('releases pending honestly when the server no longer has the task or a persisted result', async () => {
    const { state, sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    state.active = [];
    state.messages = [];
    await act(async () => { sources[0].onerror(); });
    expect(screen.getByText(/no longer running, but its result could not be recovered/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
    expect(screen.queryByText('Stopped.')).not.toBeInTheDocument();
  });

  it('times out a hanging cancellation without claiming that Stop succeeded', async () => {
    const { state } = recoveryHarness();
    const log = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<ChatMode />);
    await startRecoveryTurn();
    state.cancel = () => new Promise(() => {});
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop response'));
      fireEvent.click(screen.getByLabelText('Stop response'));
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByText(/Could not confirm Stop/)).toBeInTheDocument();
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.endsWith('/cancel'))).toHaveLength(1);
    log.mockRestore();
  });

  it('ignores a stream URL that resolves after unmount', async () => {
    const { sources } = recoveryHarness();
    let resolveUrl;
    chatHarness.buildStreamUrl.mockImplementationOnce(() => new Promise((resolve) => { resolveUrl = resolve; }));
    const view = render(<ChatMode />);
    await startRecoveryTurn();
    view.unmount();
    await act(async () => { resolveUrl('/synthetic-old-stream'); });
    expect(sources).toHaveLength(0);
  });

  it('ignores a late thread GET after newer navigation without attaching the wrong stream', async () => {
    const { sources } = recoveryHarness();
    const a = { ...thread, id: 'thread-a', title: 'Thread A' };
    const b = { ...thread, id: 'thread-b', title: 'Thread B' };
    let resolveA;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => {
      if (url === '/v1/chat/threads') return Promise.resolve({ threads: [thread, a, b] });
      if (url === '/v1/chat/threads/thread-a') return new Promise((resolve) => { resolveA = resolve; });
      if (url === '/v1/chat/threads/thread-b') return Promise.resolve({ thread: b, messages: [{ ...assistantMessage, content: 'B content' }], active_stream_ids: [] });
      return fallback(url, options);
    });
    render(<ChatMode />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'Thread A', exact: true }));
    fireEvent.click(screen.getByRole('button', { name: 'Thread B', exact: true }));
    await screen.findByText('B content');
    await act(async () => { resolveA({ thread: a, messages: [{ ...assistantMessage, content: 'Late A content' }], active_stream_ids: ['old-a-stream'] }); });
    expect(screen.getByDisplayValue('Thread B')).toBeInTheDocument();
    expect(screen.queryByText('Late A content')).not.toBeInTheDocument();
    expect(sources).toHaveLength(0);
  });

  it('does not let a delayed terminal refresh erase the next response', async () => {
    const { state, sources } = recoveryHarness();
    render(<ChatMode />);
    await startRecoveryTurn();
    state.active = [];
    vi.useFakeTimers();
    let resolveRefresh;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => url === '/v1/chat/threads/thread-1' && !options?.signal
      ? new Promise((resolve) => { resolveRefresh = resolve; }) : fallback(url, options));
    await act(async () => {
      sources[0].onmessage({ data: JSON.stringify({ done: true, content: 'First complete' }) });
      await vi.advanceTimersByTimeAsync(120);
    });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true })); });
    await act(async () => {
      sources[1].onmessage({ data: JSON.stringify({ token: 'New partial' }) });
      resolveRefresh({ thread, messages: [{ ...assistantMessage, content: 'Old snapshot' }], active_stream_ids: [] });
    });
    expect(screen.getByText('New partial')).toBeInTheDocument();
    expect(screen.queryByText('Old snapshot')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
  });

  it.each(['hang', 'network failure'])('marks initial POST %s as unconfirmed without resending or claiming Stop', async (kind) => {
    const { sources } = recoveryHarness();
    let accept;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => url.includes('/regenerate/')
      ? (kind === 'hang' ? new Promise((resolve) => { accept = resolve; }) : Promise.reject(new Error('lost acceptance response')))
      : fallback(url, options));
    render(<ChatMode />);
    await screen.findByText('Initial response');
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByText(/request has not been confirmed|Could not confirm whether the response request/)).toBeInTheDocument();
    expect(document.querySelector('.chat-cursor')).not.toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByLabelText('Stop response')); });
    expect(screen.getByText(/cancellation is not confirmed/)).toBeInTheDocument();
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.includes('/regenerate/'))).toHaveLength(1);
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.endsWith('/cancel'))).toHaveLength(0);
    expect(sources).toHaveLength(0);
    // Keep the deliberately unresolved acceptance fixture local to this test.
    expect(kind === 'hang' ? typeof accept : 'unused').toBe(kind === 'hang' ? 'function' : 'unused');
  });

  it.each([false, true])('REVIEW treats a missing accepted stream ID as unknown with queued Stop=%s', async (queuedStop) => {
    const { sources } = recoveryHarness();
    let accept;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => url.includes('/regenerate/')
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    render(<ChatMode />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true }));
    if (queuedStop) fireEvent.click(screen.getByLabelText('Stop response'));
    await act(async () => { accept({ thread, messages: [{ ...assistantMessage, content: '' }] }); });
    if (!queuedStop && sources[0]) await act(async () => { sources[0].onerror(); });
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.endsWith('/cancel'))).toHaveLength(0);
    expect(sources).toHaveLength(0);
    expect(screen.queryByText(/no longer running/)).not.toBeInTheDocument();
  });

  it('locks generation ownership while a fork POST is pending', async () => {
    const { state, sources } = recoveryHarness();
    state.messages = [{ id: 'user-1', role: 'user', content: 'Prompt', status: 'complete', metadata: {} }, assistantMessage];
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('Edited prompt');
    let accept;
    const fallback = chatHarness.apiFetch.getMockImplementation();
    chatHarness.apiFetch.mockImplementation((url, options) => url.endsWith('/fork')
      ? new Promise((resolve) => { accept = resolve; }) : fallback(url, options));
    render(<ChatMode />);
    await screen.findByText('Initial response');
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit', exact: true })[1]);
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole('button', { name: 'Regenerate', exact: true })[0]);
    expect(chatHarness.apiFetch.mock.calls.filter(([url]) => url.includes('/regenerate/'))).toHaveLength(0);
    await act(async () => { accept({ thread: { ...thread, id: 'branch' }, messages: state.messages, stream_id: 'fork-stream' }); });
    expect(sources).toHaveLength(1);
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    prompt.mockRestore();
  });

});


describe('ToolUseCard truthful missing detail and false results', () => {
  it('distinguishes omitted details from an empty tool value', () => {
    render(<ToolUseCard tool={{ tool_name: 'memory', status: 'ok' }} />);
    fireEvent.click(screen.getByRole('button', { name: /memory/i }));
    expect(screen.getAllByText('Details unavailable')).toHaveLength(2);
    expect(screen.queryByText('(empty)')).not.toBeInTheDocument();
  });
  it.each([false, 0])('retains a legitimate false-like tool result: %s', (value) => {
    render(<ToolUseCard tool={{ tool_name: 'synthetic', status: 'ok', tool_input: {}, tool_output: value }} />);
    fireEvent.click(screen.getByRole('button', { name: /synthetic/i }));
    expect(screen.getByText(String(value))).toBeInTheDocument();
  });
  it('does not report completion when the status is absent', () => {
    render(<ToolUseCard tool={{ tool_name: 'synthetic' }} />);
    const control = screen.getByRole('button', { name: /synthetic/i });
    expect(control).toHaveTextContent('Status unavailable');
    expect(control).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(control);
    expect(control).toHaveAttribute('aria-expanded', 'true');
  });
});
