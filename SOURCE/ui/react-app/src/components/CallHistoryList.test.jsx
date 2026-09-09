import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import CallHistoryList from './CallHistoryList';
import { fetchCallHistory, getCallTranscript } from '../hooks/useCallAudio';

vi.mock('../hooks/useCallAudio', () => ({
  fetchCallHistory: vi.fn(),
  getCallTranscript: vi.fn(),
}));

const firstCall = {
  call_id: 'call-1',
  phone_number: '+1 555 0100',
  status: 'completed',
  duration_seconds: 95,
  started_at: '2026-05-09T17:00:00Z',
  summary: 'Checked the order.',
  has_recording: true,
};

describe('CallHistoryList', () => {
  beforeEach(() => {
    fetchCallHistory.mockReset();
    getCallTranscript.mockReset();
  });

  it('fetches and renders call rows', async () => {
    fetchCallHistory.mockResolvedValueOnce({ count: 1, calls: [firstCall] });

    render(<CallHistoryList />);

    expect(await screen.findByText('+1 555 0100')).toBeInTheDocument();
    expect(screen.getByText('completed')).toBeInTheDocument();
    expect(screen.getByText('Recording')).toBeInTheDocument();
    expect(fetchCallHistory).toHaveBeenCalledWith(50, 0);
  });

  it('loads additional pages from the current offset', async () => {
    fetchCallHistory
      .mockResolvedValueOnce({
        count: 2,
        calls: [
          firstCall,
          { ...firstCall, call_id: 'call-2', phone_number: '+1 555 0101' },
        ],
      })
      .mockResolvedValueOnce({
        count: 1,
        calls: [{ ...firstCall, call_id: 'call-3', phone_number: '+1 555 0102' }],
      });

    const { user } = render(<CallHistoryList pageSize={2} />);

    expect(await screen.findByText('+1 555 0101')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /load more/i }));

    expect(await screen.findByText('+1 555 0102')).toBeInTheDocument();
    expect(fetchCallHistory).toHaveBeenNthCalledWith(2, 2, 2);
  });

  it('expands a row and fetches the transcript', async () => {
    fetchCallHistory.mockResolvedValueOnce({ count: 1, calls: [firstCall] });
    getCallTranscript.mockResolvedValueOnce({
      call_id: 'call-1',
      transcript: [
        { role: 'them', text: 'Where is my order?', ts: 1 },
        { role: 'viola', text: 'It arrives tomorrow.', ts: 2 },
      ],
    });

    const { user } = render(<CallHistoryList />);

    await user.click(await screen.findByText('+1 555 0100'));

    expect(await screen.findByText('Where is my order?')).toBeInTheDocument();
    expect(screen.getByText('It arrives tomorrow.')).toBeInTheDocument();
    const recording = screen.getByLabelText('Recording for +1 555 0100');
    expect(recording).toHaveStyle({ colorScheme: 'dark' });
    expect(getCallTranscript).toHaveBeenCalledWith('call-1');

    // Own-side-right convention (same as the live call transcript panel):
    // Viola (own side) right, the recipient ("them") left.
    const violaBubbleRow = screen.getByText('It arrives tomorrow.').closest('[style]').parentElement;
    expect(violaBubbleRow).toHaveStyle({ alignItems: 'flex-end' });
    const themBubbleRow = screen.getByText('Where is my order?').closest('[style]').parentElement;
    expect(themBubbleRow).toHaveStyle({ alignItems: 'flex-start' });
  });

  it('renders an empty state', async () => {
    fetchCallHistory.mockResolvedValueOnce({ count: 0, calls: [] });

    render(<CallHistoryList />);

    await waitFor(() => expect(screen.getByTestId('call-history-empty')).toBeInTheDocument());
    expect(screen.getByText('No call history yet.')).toBeInTheDocument();
  });

  it('a React key change (the SmartDisplay:2754 principal key, sibling of the C-071 ChatMode fix) remounts and refetches, clearing the previous key\'s rows', async () => {
    // `loadPage` is a `useCallback(..., [pageSize])` and its boot effect fires
    // exactly once for the component's life -- SmartDisplay mounts this with
    // no key at all pre-fix, so an in-place desktop sign-out/sign-in never
    // remounts it and the new account's phone panel keeps rendering the
    // previous account's call history. SmartDisplay now mounts it with
    // `key={accountUser?.id || 'device'}`; this pins that a key change (what
    // a principal switch produces) is what actually clears the old account's
    // rows -- an unkeyed mount (no key prop at all) would instead keep the
    // same instance and never re-fetch, silently carrying user A's rows into
    // user B's view.
    const userACall = { ...firstCall, call_id: 'call-user-a', phone_number: '+1 555 0100' };
    const userBCall = { ...firstCall, call_id: 'call-user-b', phone_number: '+1 555 0200' };

    fetchCallHistory
      .mockResolvedValueOnce({ count: 1, calls: [userACall] })
      .mockResolvedValueOnce({ count: 1, calls: [userBCall] });

    const { rerender } = render(<CallHistoryList key="user-a" />);
    expect(await screen.findByText('+1 555 0100')).toBeInTheDocument();

    // The account switch: SmartDisplay re-renders with a new principal key.
    rerender(<CallHistoryList key="user-b" />);

    expect(await screen.findByText('+1 555 0200')).toBeInTheDocument();
    expect(screen.queryByText('+1 555 0100')).not.toBeInTheDocument();
    expect(fetchCallHistory).toHaveBeenCalledTimes(2);
  });
});
