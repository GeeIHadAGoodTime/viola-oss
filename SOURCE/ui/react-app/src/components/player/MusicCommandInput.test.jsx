import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useViolaApi } from '../../hooks/useViolaApi';
import MusicCommandInput from './MusicCommandInput';

vi.mock('../../config', () => ({
  getClientApiKey: vi.fn(async () => ''),
  getClientApiKeySync: vi.fn(() => ''),
  getCloudAccessToken: vi.fn(() => ''),
}));
vi.mock('../../lib/gotrue_client', () => ({
  getGoTrueAccessToken: vi.fn(async () => ''),
}));

const failureMessage = "We couldn't complete that request. Your text is still here; please try again.";
const input = () => screen.getByRole('textbox', { name: 'Ask Viola to play something' });
const send = () => screen.getByRole('button', { name: 'Send music request' });

function ApiComposer() {
  const api = useViolaApi();
  return <MusicCommandInput onSubmit={api.sendCommand} />;
}

describe('MusicCommandInput submission recovery', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([400, 503])('retains the draft and permits a successful retry after HTTP %i', async status => {
    fetch.mockResolvedValueOnce({
      ok: false, status,
      text: async () => JSON.stringify({ error: { code: 'synthetic_failure', message: 'Private diagnostic' } }),
    }).mockResolvedValueOnce({
      ok: true, status: 200, json: async () => ({ ok: true, data: { message: '56' } }),
    });
    const user = userEvent.setup();
    render(<ApiComposer />);
    await user.type(input(), 'What is 7 times 8?');
    await user.click(send());

    expect(await screen.findByRole('alert')).toHaveTextContent(failureMessage);
    expect(input()).toHaveValue('What is 7 times 8?');
    expect(input()).toHaveAccessibleDescription(failureMessage);
    expect(send()).toBeEnabled();
    expect(screen.queryByText('Private diagnostic')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);

    await user.click(send());
    await waitFor(() => expect(input()).toHaveValue(''));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(input()).not.toHaveAttribute('aria-describedby');
    expect(fetch).toHaveBeenCalledTimes(2);
    for (const [url, options] of fetch.mock.calls) {
      expect(url).toBe('/v1/command');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({ text: 'What is 7 times 8?', history: [] });
    }
  });

  it('shows a useful error after network rejection, with no automatic retry', async () => {
    fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    render(<ApiComposer />);
    fireEvent.change(input(), { target: { value: 'What is 8 times 9?' } });
    fireEvent.submit(input().closest('form'));
    expect(await screen.findByRole('alert')).toHaveTextContent(failureMessage);
    expect(input()).toHaveValue('What is 8 times 9?');
    expect(send()).toBeEnabled();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('contains a synchronous submit failure and clears stale error while an explicit retry is pending', async () => {
    let resolveRetry;
    const onSubmit = vi.fn()
      .mockImplementationOnce(() => { throw new Error('private synchronous failure'); })
      .mockImplementationOnce(() => new Promise(resolve => { resolveRetry = resolve; }));
    render(<MusicCommandInput onSubmit={onSubmit} />);
    fireEvent.change(input(), { target: { value: 'pause' } });
    fireEvent.submit(input().closest('form'));
    expect(await screen.findByRole('alert')).toHaveTextContent(failureMessage);
    fireEvent.submit(input().closest('form'));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(input()).toHaveValue('pause');
    expect(send()).toBeDisabled();
    fireEvent.submit(input().closest('form'));
    expect(onSubmit).toHaveBeenCalledTimes(2);
    await act(async () => resolveRetry());
    expect(input()).toHaveValue('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the existing successful Enter submission, trimming and clear behavior', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue({ message: '56' });
    render(<MusicCommandInput onSubmit={onSubmit} />);
    await user.type(input(), '  What is 7 times 8?  {Enter}');
    await waitFor(() => expect(input()).toHaveValue(''));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith('What is 7 times 8?');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not submit empty or disabled input', () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<MusicCommandInput onSubmit={onSubmit} />);
    fireEvent.change(input(), { target: { value: '   ' } });
    fireEvent.submit(input().closest('form'));
    expect(send()).toBeDisabled();
    rerender(<MusicCommandInput onSubmit={onSubmit} disabled />);
    fireEvent.change(input(), { target: { value: 'pause' } });
    fireEvent.submit(input().closest('form'));
    expect(input()).toBeDisabled();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
