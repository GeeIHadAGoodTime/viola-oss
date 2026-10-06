import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import QueueModal from './QueueModal';
import wire from './fixtures/queueWireContract.json';

const apiFacade = vi.hoisted(() => ({ copy: 0 }));
vi.mock('../hooks/useViolaApi', async (importOriginal) => {
  const original = await importOriginal();
  return {
    ...original,
    useViolaApi: () => {
      const api = original.useViolaApi();
      if (apiFacade.copy > 0) {
        apiFacade.copy -= 1;
        return { ...api };
      }
      return api;
    },
  };
});

vi.mock('../lib/gotrue_client', () => ({ getGoTrueAccessToken: vi.fn(async () => '') }));

const response = (value) => ({
  ok: value.status >= 200 && value.status < 300,
  status: value.status,
  json: async () => structuredClone(value.body),
  text: async () => JSON.stringify(value.body),
});

const deferred = () => {
  let resolve;
  const promise = new Promise((accept) => { resolve = accept; });
  return { promise, resolve };
};
const emptyResponse = () => {
  const value = structuredClone(wire.get_success);
  value.body.data.queue = [];
  return response(value);
};
const track = wire.get_success.body.data.queue[0];

beforeEach(() => { apiFacade.copy = 0; window.__VIOLA_API_KEY__ = 'synthetic-queue-read-fixture'; });
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete window.__VIOLA_API_KEY__;
});

describe('Queue initial-read recovery through the actual response adapter', () => {
  it('shows the serialized GET failure without claiming the queue is empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(wire.get_refused)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');
    expect(screen.queryByText('Queue is empty. Ask me to play some music!')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry queue' })).toBeEnabled();
  });

  it('accepts a confirmed empty queue from the canonical success envelope', async () => {
    const empty = structuredClone(wire.get_success);
    empty.body.data.queue = [];
    vi.stubGlobal('fetch', vi.fn(async () => response(empty)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('accepts the captured nonempty GET response without an invented inner success flag', async () => {
    expect(wire.get_success.body.data).not.toHaveProperty('ok');
    vi.stubGlobal('fetch', vi.fn(async () => response(wire.get_success)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByText(wire.get_success.body.data.queue[0].title)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('retries a failed read and displays the newly confirmed queue', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(wire.get_refused)).mockResolvedValueOnce(response(wire.get_success));
    vi.stubGlobal('fetch', fetch);
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry queue' })));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.getByText(track.title)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('coalesces repeated same-turn Retry clicks until that read settles', async () => {
    const pending = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(response(wire.get_refused)).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetch);
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    const retry = screen.getByRole('button', { name: 'Retry queue' });
    await act(async () => { fireEvent.click(retry); fireEvent.click(retry); });
    expect(fetch).toHaveBeenCalledTimes(2);expect(screen.getByText('Loading queue...')).toBeInTheDocument();
    await act(async () => pending.resolve(emptyResponse()));
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();
  });

  it.each([undefined, null, false, 'invalid', {}])('does not treat malformed queue %s as known empty', async (queue) => {
    const value = structuredClone(wire.get_success);value.body.data.queue = queue;
    vi.stubGlobal('fetch', vi.fn(async () => response(value)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');
    expect(screen.queryByText('Queue is empty. Ask me to play some music!')).not.toBeInTheDocument();
  });

  it('retains explicit refusal even if its HTTP status is incorrectly successful', async () => {
    const refused = { ...wire.get_refused, status: 200 };
    vi.stubGlobal('fetch', vi.fn(async () => response(refused)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');
  });

  it('shows a transport rejection and retains Retry', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Synthetic unavailable transport')));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');
    expect(screen.getByRole('button', { name: 'Retry queue' })).toBeEnabled();
  });

  it('bounds a hanging read and retires its late success after a newer Retry', async () => {
    vi.useFakeTimers();const old = deferred();
    const fetch = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(emptyResponse());vi.stubGlobal('fetch', fetch);
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('alert')).toHaveTextContent('in time');
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry queue' })));
    await act(async () => old.resolve(response(wire.get_success)));
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('rejects a read settling beyond its deadline before the timer callback executes', async () => {
    const pending = deferred();vi.stubGlobal('fetch', vi.fn(() => pending.promise));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    const now = performance.now();vi.spyOn(performance, 'now').mockReturnValue(now + 15001);
    await act(async () => pending.resolve(response(wire.get_success)));
    expect(screen.getByRole('alert')).toHaveTextContent('in time');expect(screen.queryByText(track.title)).not.toBeInTheDocument();
  });

  it('keeps a Retry failure visible without declaring an empty queue', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(wire.get_refused)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry queue' })));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');
    expect(screen.queryByText('Queue is empty. Ask me to play some music!')).not.toBeInTheDocument();
  });

  it('does not publish an old read failure into a reopened modal', async () => {
    const old = deferred();vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(wire.get_success)));
    const view = render(<QueueModal isOpen onClose={() => {}} />);
    await act(async () => view.rerender(<QueueModal isOpen={false} onClose={() => {}} />));
    await act(async () => view.rerender(<QueueModal isOpen onClose={() => {}} />));
    await act(async () => old.resolve(response(wire.get_refused)));
    expect(screen.getByText(track.title)).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('retires a read immediately on Close before the parent changes its open prop', async () => {
    const old = deferred();const close = vi.fn();vi.stubGlobal('fetch', vi.fn(() => old.promise));
    await act(async () => render(<QueueModal isOpen onClose={close} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Close', exact: true })));
    await act(async () => old.resolve(response(wire.get_refused)));
    expect(close).toHaveBeenCalledTimes(1);expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps an authoritative empty snapshot usable after an older HTTP failure', async () => {
    const old = deferred();vi.stubGlobal('fetch', vi.fn(() => old.promise));
    const view = render(<QueueModal isOpen onClose={() => {}} />);
    await act(async () => view.rerender(<QueueModal isOpen onClose={() => {}} wsQueue={[]} />));
    await act(async () => old.resolve(response(wire.get_refused)));
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not obscure an authoritative nonempty snapshot with fallback failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(wire.get_refused)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} wsQueue={[track]} />));
    expect(screen.getByText(track.title)).toBeInTheDocument();expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('accepted Clear retires an older pending read before it can restore removed rows', async () => {
    const old = deferred();vi.stubGlobal('fetch', vi.fn(async (_url, options) => options.method ? response(wire.clear_success) : old.promise));
    const view = render(<QueueModal isOpen onClose={() => {}} wsQueue={[track]} />);
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Clear Queue' })));
    await act(async () => view.rerender(<QueueModal isOpen onClose={() => {}} />));
    await act(async () => old.resolve(response(wire.get_success)));
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();expect(screen.queryByText(track.title)).not.toBeInTheDocument();
  });

  it('a failed action refresh retains the last confirmed HTTP rows and offers Retry', async () => {
    let reads = 0;
    vi.stubGlobal('fetch', vi.fn(async (_url, options) => options.method ? response(wire.play_success) : response(++reads === 1 ? wire.get_success : wire.get_refused)));
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: `Play ${track.title} now` })));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');expect(screen.getByText(track.title)).toBeInTheDocument();
  });

  it('cleans a pending read deadline on unmount', async () => {
    vi.useFakeTimers();const old = deferred();vi.stubGlobal('fetch', vi.fn(() => old.promise));
    const view = render(<QueueModal isOpen onClose={() => {}} />);
    await act(async () => {});expect(vi.getTimerCount()).toBeGreaterThan(0);
    view.unmount();await act(async () => vi.advanceTimersByTimeAsync(0));expect(vi.getTimerCount()).toBe(0);
    await act(async () => old.resolve(response(wire.get_success)));
  });

  it('does not treat a prior opening snapshot as a confirmed read after reopening fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(wire.get_success)).mockResolvedValueOnce(response(wire.get_refused)));
    const view = render(<QueueModal isOpen onClose={() => {}} />);
    await act(async () => {});expect(screen.getByText(track.title)).toBeInTheDocument();
    await act(async () => view.rerender(<QueueModal isOpen={false} onClose={() => {}} />));
    await act(async () => view.rerender(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the queue');
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();
    expect(screen.queryByText('Queue is empty. Ask me to play some music!')).not.toBeInTheDocument();
  });

  it('keeps one read subscription when only the API facade identity changes', async () => {
    apiFacade.copy = 3;
    const fetch = vi.fn(async () => response(wire.get_success));
    vi.stubGlobal('fetch', fetch);
    await act(async () => render(<QueueModal isOpen onClose={() => {}} />));
    expect(screen.getByText(track.title)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

});
