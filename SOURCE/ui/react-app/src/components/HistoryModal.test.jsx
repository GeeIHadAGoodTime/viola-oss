import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import HistoryModal from './HistoryModal';
import ChatSidebar from './stage/modes/chat/ChatSidebar';
import { apiFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({ apiFetch: vi.fn() }));
const thread = { id: 'saved-1', title: 'Weekend plans', updated_at: 1791028800 };
const messages = [
  { id: 'user-1', role: 'user', content: 'Plan a quiet weekend' },
  { id: 'reply-1', role: 'assistant', content: 'Visit the garden' },
];
const props = { isOpen: true, onClose: vi.fn(), principalKey: 'account-a' };
beforeEach(() => {
  vi.clearAllMocks();
  apiFetch.mockImplementation((url) => Promise.resolve(
    url === '/v1/chat/threads' ? { threads: [thread] } : { thread, messages },
  ));
});
describe('HistoryModal saved conversations', () => {
  it('shows the durable thread visible in the sidebar even with no transient activity', async () => {
    render(<><ChatSidebar threads={[thread]} activeThreadId={thread.id} search="" collapsed={false}
      onSearch={vi.fn()} onToggleCollapsed={vi.fn()} onNewChat={vi.fn()} onSelectThread={vi.fn()}
      onRename={vi.fn()} onDelete={vi.fn()} onExport={vi.fn()} />
    <HistoryModal {...props} /></>);
    const dialog = screen.getByRole('dialog', { name: 'Chat History' });
    fireEvent.click(await within(dialog).findByRole('button', { name: thread.title }));
    expect(await within(dialog).findByText('Visit the garden')).toBeInTheDocument();
    expect(within(dialog).getByText('Plan a quiet weekend')).toBeInTheDocument();
    expect(within(dialog).queryByText('No chat history yet. Start a conversation!')).not.toBeInTheDocument();
  });
  it('distinguishes loading from an empty saved list', async () => {
    let resolve;
    apiFetch.mockReturnValue(new Promise(r => { resolve = r; }));
    render(<HistoryModal {...props} />);
    expect(screen.getByText('Loading saved chats...')).toBeInTheDocument();
    expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
    await act(async () => resolve({ threads: [] }));
    expect(screen.getByText('No saved chats yet.')).toBeInTheDocument();
  });
  it('offers retry after saved-list failure instead of reporting an empty history', async () => {
    apiFetch.mockRejectedValueOnce(new Error('offline'));
    render(<HistoryModal {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load saved chats');
    expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry saved chats' }));
    expect(await screen.findByRole('button', { name: thread.title })).toBeInTheDocument();
  });
  it('shows a consent explanation without claiming saved chats are empty', async () => {
    apiFetch.mockRejectedValueOnce(Object.assign(new Error('consent'), { code: 'consent_required' }));
    render(<HistoryModal {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Settings → Account → Privacy & Data');
    expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
  });
  it('ignores an older conversation response after selecting another chat', async () => {
    let resolveFirst;
    const other = { id: 'saved-2', title: 'Another chat' };
    apiFetch.mockImplementation((url) => {
      if (url === '/v1/chat/threads') return Promise.resolve({ threads: [thread, other] });
      if (url.endsWith('/saved-1')) return new Promise(r => { resolveFirst = r; });
      return Promise.resolve({ thread: other, messages: [{ id: 'm2', role: 'assistant', content: 'Newest selection' }] });
    });
    render(<HistoryModal {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: thread.title }));
    expect(screen.getByText('Loading conversation...')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: other.title }));
    expect(await screen.findByText('Newest selection')).toBeInTheDocument();
    await act(async () => resolveFirst({ thread, messages }));
    expect(screen.queryByText('Visit the garden')).not.toBeInTheDocument();
    expect(screen.getByText('Newest selection')).toBeInTheDocument();
  });
  it('retries a failed conversation without changing the selection', async () => {
    apiFetch.mockImplementationOnce(() => Promise.resolve({ threads: [thread] }))
      .mockRejectedValueOnce(new Error('offline'));
    render(<HistoryModal {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: thread.title }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load this conversation');
    fireEvent.click(screen.getByRole('button', { name: 'Retry conversation' }));
    expect(await screen.findByText('Visit the garden')).toBeInTheDocument();
  });
  it('drops old account data immediately and ignores late account responses', async () => {
    let resolveOld;
    const { rerender } = render(<HistoryModal {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: thread.title }));
    expect(await screen.findByText('Visit the garden')).toBeInTheDocument();
    apiFetch.mockReturnValueOnce(new Promise(r => { resolveOld = r; }));
    rerender(<HistoryModal {...props} principalKey="account-b" />);
    expect(screen.queryByText('Visit the garden')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: thread.title })).not.toBeInTheDocument();
    apiFetch.mockResolvedValueOnce({ threads: [{ id: 'device-1', title: 'Device chat' }] });
    rerender(<HistoryModal {...props} principalKey="device" />);
    expect(await screen.findByRole('button', { name: 'Device chat' })).toBeInTheDocument();
    await act(async () => resolveOld({ threads: [thread] }));
    expect(screen.queryByRole('button', { name: thread.title })).not.toBeInTheDocument();
  });
  it('discards an old account conversation that resolves after an identity switch', async () => {
    let resolveOld;
    apiFetch.mockImplementationOnce(() => Promise.resolve({ threads: [thread] }))
      .mockImplementationOnce(() => new Promise(r => { resolveOld = r; }));
    const { rerender } = render(<HistoryModal {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: thread.title }));
    apiFetch.mockResolvedValueOnce({ threads: [] });
    rerender(<HistoryModal {...props} principalKey="account-b" />);
    await screen.findByText('No saved chats yet.');
    await act(async () => resolveOld({ thread, messages }));
    expect(screen.queryByText('Visit the garden')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: thread.title })).not.toBeInTheDocument();
  });
  it('keeps a selected empty conversation stable on repeated selection', async () => {
    apiFetch.mockImplementationOnce(() => Promise.resolve({ threads: [thread] }))
      .mockResolvedValueOnce({ thread, messages: [] });
    render(<HistoryModal {...props} />);
    const button = await screen.findByRole('button', { name: thread.title });
    fireEvent.click(button);
    expect(await screen.findByText('No messages in this conversation yet.')).toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.queryByText('Loading conversation...')).not.toBeInTheDocument();
    expect(apiFetch.mock.calls.filter(([url]) => url.endsWith('/saved-1'))).toHaveLength(1);
  });
  it('preserves transient voice activity, filters and bounded clearing', async () => {
    const onClearHistory = vi.fn();
    render(<HistoryModal {...props} history={[
      { role: 'user', content: 'Pause music' }, { role: 'assistant', content: 'Paused' },
    ]} onClearHistory={onClearHistory} />);
    await screen.findByRole('button', { name: thread.title });
    fireEvent.click(screen.getByRole('button', { name: 'Filter by commands' }));
    expect(screen.getByText('Pause music')).toBeInTheDocument();
    expect(screen.queryByText('Paused')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear recent activity' }));
    expect(onClearHistory).toHaveBeenCalledTimes(1);
    expect(apiFetch.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true);
  });
  it('does not fetch while closed and refreshes saved chats when reopened', async () => {
    const { rerender } = render(<HistoryModal {...props} isOpen={false} />);
    expect(apiFetch).not.toHaveBeenCalled();
    rerender(<HistoryModal {...props} />);
    await screen.findByRole('button', { name: thread.title });
    rerender(<HistoryModal {...props} isOpen={false} />);
    apiFetch.mockResolvedValueOnce({ threads: [] });
    rerender(<HistoryModal {...props} />);
    expect(await screen.findByText('No saved chats yet.')).toBeInTheDocument();
    expect(apiFetch.mock.calls.filter(([url]) => url === '/v1/chat/threads')).toHaveLength(2);
  });
  it('closes with Escape and returns focus to the opening control', async () => {
    const onClose = vi.fn();
    const { rerender } = render(<><button>History opener</button><HistoryModal {...props} isOpen={false} onClose={onClose} /></>);
    const opener = screen.getByRole('button', { name: 'History opener' });
    opener.focus();
    rerender(<><button>History opener</button><HistoryModal {...props} onClose={onClose} /></>);
    await screen.findByRole('button', { name: thread.title });
    await waitFor(() => expect(screen.getByRole('dialog')).toContainElement(document.activeElement));
    fireEvent.keyDown(document.activeElement, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    rerender(<><button>History opener</button><HistoryModal {...props} isOpen={false} onClose={onClose} /></>);
    expect(opener).toHaveFocus();
  });
});
