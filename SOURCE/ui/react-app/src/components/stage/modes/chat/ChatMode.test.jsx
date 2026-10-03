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
    await waitFor(() => expect(screen.getByText('The live response connection dropped.')).toBeInTheDocument());
    // Flush the immediate async refresh that previously erased the transient failure.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText('The live response connection dropped.')).toBeInTheDocument();
    expect(source.close).toHaveBeenCalled();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
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
          ? Promise.reject(new Error('send unavailable'))
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
      ? 'The live response connection dropped.'
      : 'Something went wrong while sending that message.');
    expect(screen.getByText('first message')).toBeInTheDocument();
    expect(screen.queryByLabelText('Stop response')).not.toBeInTheDocument();
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
