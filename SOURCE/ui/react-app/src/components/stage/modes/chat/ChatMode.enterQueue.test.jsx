/**
 * 1.0.5 build 11 laptop run, typed in the Chat tab:
 * - bug 7: Enter while a reply ran pressed Stop and left the draft in the box,
 *   so "pause" then "next" went out as one message, "pausenext".
 * - bug 9: "open the memory panel" opened nothing; the Chat tab never ran the
 *   on-screen action the tool asked for.
 * - bug 16: a bulleted answer showed as one run-together paragraph.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../../../test/test-utils';
import ChatMode from './ChatMode';
import ChatInput from './ChatInput';
import Markdown from './markdown';

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

const thread = { id: 'thread-1', title: 'Existing thread', updated_at: 1778529600 };

function harness() {
  const sources = [];
  const sent = [];
  const cancels = [];
  window.EventSource = class {
    constructor(url) {
      this.url = url;
      sources.push(this);
    }

    close = vi.fn();
  };
  chatHarness.buildStreamUrl.mockImplementation(async (streamId) => `/api/stream/${streamId}`);
  chatHarness.useWebSocket.mockReturnValue({});
  chatHarness.apiFetch.mockImplementation((url, options = {}) => {
    if (url === '/v1/chat/models') {
      return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
    }
    if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
      return Promise.resolve({ threads: [thread] });
    }
    if (url === '/v1/chat/threads/thread-1') {
      return Promise.resolve({ thread, messages: [], active_stream_ids: [] });
    }
    if (url === '/v1/chat/threads/thread-1/send' && options.method === 'POST') {
      const body = JSON.parse(options.body);
      sent.push(body.text);
      return Promise.resolve({ stream_id: `stream-${sent.length}`, thread, user_message: { id: `u-${sent.length}`, role: 'user', content: body.text } });
    }
    if (url.endsWith('/cancel')) {
      cancels.push(url);
      return Promise.resolve({ cancelled: true });
    }
    return Promise.resolve({});
  });
  return { sources, sent, cancels };
}

async function typeAndEnter(text) {
  const box = screen.getByRole('textbox', { name: 'Message Viola' });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: 'Enter' });
}

describe('ChatMode: Enter never stops a reply', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    delete window.EventSource;
  });

  it('queues the text typed mid-reply as its own message and sends it when the reply ends', async () => {
    const { sources, sent, cancels } = harness();
    render(<ChatMode />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('gpt-test'));

    await act(async () => { await typeAndEnter('pause'); });
    await waitFor(() => expect(sources).toHaveLength(1));
    expect(sent).toEqual(['pause']);
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();

    await act(async () => { await typeAndEnter('next'); });

    // Nothing stopped, and the box is clear (the draft is not kept to be glued on).
    expect(cancels).toEqual([]);
    expect(screen.getByLabelText('Stop response')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message Viola' })).toHaveValue('');
    expect(screen.getByText(/Sends when this reply ends: next/)).toBeInTheDocument();
    expect(sent).toEqual(['pause']);

    await act(async () => {
      sources[0].onmessage({ data: JSON.stringify({ done: true, content: 'Paused.' }) });
    });

    await waitFor(() => expect(sent).toEqual(['pause', 'next']));
    expect(sent).not.toContain('pausenext');
    expect(cancels).toEqual([]);
  });

  it('Stop is the button: clicking it while a reply runs asks the server to stop', async () => {
    const { sources, cancels } = harness();
    render(<ChatMode />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('gpt-test'));
    await act(async () => { await typeAndEnter('open notepad and type a long paragraph'); });
    await waitFor(() => expect(sources).toHaveLength(1));

    await act(async () => { fireEvent.click(screen.getByLabelText('Stop response')); });

    await waitFor(() => expect(cancels).toEqual(['/v1/chat/streams/stream-1/cancel']));
  });

  it('runs the on-screen action carried in the final stream event, with one reply bubble', async () => {
    const { sources } = harness();
    const seen = [];
    const listener = (event) => seen.push(event.detail);
    window.addEventListener('viola:ui-action', listener);
    try {
      render(<ChatMode />);
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('gpt-test'));
      await act(async () => { await typeAndEnter('open the memory panel'); });
      await waitFor(() => expect(sources).toHaveLength(1));

      await act(async () => {
        sources[0].onmessage({
          data: JSON.stringify({
            done: true,
            content: 'Opening your memory panel.',
            ui_actions: {
              ui_action: 'open_memory_panel',
              panel_id: 'memory',
              ai_data: { command_results: { routed: { data: { ui_action: 'open_memory_panel', panel_id: 'memory' } } } },
            },
          }),
        });
      });

      expect(seen.map((item) => item.action)).toEqual(['open_memory_panel']);
      expect(screen.getAllByText('Opening your memory panel.')).toHaveLength(1);
    } finally {
      window.removeEventListener('viola:ui-action', listener);
    }
  });
});

describe('ChatInput keys', () => {
  const renderInput = (props) => render(
    <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} onAttachFiles={vi.fn()} {...props} />,
  );

  it('Enter while streaming queues the text and never presses Stop', () => {
    const onStop = vi.fn();
    const onQueue = vi.fn();
    const onSend = vi.fn();
    renderInput({ value: 'next', streaming: true, onStop, onQueue, onSend });
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message Viola' }), { key: 'Enter' });
    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(onQueue).toHaveBeenCalledWith('next');
  });

  it('Enter on an empty box while streaming does nothing', () => {
    const onStop = vi.fn();
    const onQueue = vi.fn();
    renderInput({ value: '  ', streaming: true, onStop, onQueue });
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message Viola' }), { key: 'Enter' });
    expect(onStop).not.toHaveBeenCalled();
    expect(onQueue).not.toHaveBeenCalled();
  });

  it('Enter when idle sends', () => {
    const onSend = vi.fn();
    renderInput({ value: 'hello', onSend });
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message Viola' }), { key: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});

describe('Chat answers keep their line breaks', () => {
  it('shows each line of a list whose markers were removed on its own line', () => {
    const { container } = render(<Markdown content={'Here is what I found:\nApples\nPears\nPlums'} />);
    const paragraph = container.querySelector('.chat-markdown p');
    expect(paragraph.querySelectorAll('br')).toHaveLength(3);
    expect(paragraph.textContent).toBe('Here is what I found:ApplesPearsPlums');
  });
});
