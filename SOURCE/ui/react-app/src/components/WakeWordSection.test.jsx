import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../test/test-utils';
import WakeWordSection from './WakeWordSection';
import { authFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({
  authFetch: vi.fn(),
  apiFetch: vi.fn(() => Promise.resolve({})),
}));

vi.mock('../utils/featureSurface', async (importOriginal) => ({
  ...(await importOriginal()),
  isFeatureHidden: () => false,
}));

const VIOLA = {
  model_id: 'default',
  name: 'Viola',
  is_default: true,
  is_active: true,
  is_supported: true,
  architecture: 'temporal_cnn',
};

function reply(data, { ok = true, status = 200, error } = {}) {
  return Promise.resolve({
    ok,
    status,
    json: () => Promise.resolve(ok ? { ok: true, data } : { ok: false, error }),
  });
}

function backend(overrides = {}) {
  const routes = {
    '/v1/wake/models': () => reply({ models: [VIOLA], active_model_path: 'viola.onnx' }),
    '/v1/wake/training/status': () => reply({ available: true, message: null }),
    '/v1/wake/jobs/current': () => reply({ job: null }),
    '/v1/wake/check-word': () => reply({ ok: true, reason: null, code: null }),
    ...overrides,
  };
  authFetch.mockImplementation((path) => {
    const handler = routes[path];
    if (!handler) throw new Error(`unexpected request ${path}`);
    return handler();
  });
}

describe('WakeWordSection custom training', () => {
  beforeEach(() => {
    authFetch.mockReset();
  });

  it('offers Create when the service says a job could finish', async () => {
    backend();
    render(<WakeWordSection />);
    const button = await screen.findByRole('button', { name: 'Create' });
    expect(button).toBeEnabled();
  });

  it('says why when training is down, and keeps the word list usable', async () => {
    backend({
      '/v1/wake/training/status': () =>
        reply({ available: false, message: 'Training is down on our side right now.' }),
    });
    render(<WakeWordSection />);

    expect(await screen.findByText(/Training is down on our side right now/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unavailable' })).toBeDisabled();
    expect((await screen.findAllByText('Viola')).length).toBeGreaterThan(0);
  });

  it('asks a signed-out user to sign in instead of failing later', async () => {
    backend({
      '/v1/wake/training/status': () =>
        reply({ available: false, message: 'Sign in to your Viola account to train a wake word.' }),
    });
    render(<WakeWordSection />);
    expect(await screen.findByText(/Sign in to your Viola account/)).toBeInTheDocument();
  });

  it('refuses a bad word in plain words before anything is recorded', async () => {
    backend({
      '/v1/wake/check-word': () =>
        reply({ ok: false, reason: '"no" is too short to recognize reliably.', code: 'too_short' }),
    });
    render(<WakeWordSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Create' }));

    const input = screen.getByPlaceholderText('e.g. Athena');
    fireEvent.change(input, { target: { value: 'no' } });
    fireEvent.blur(input);

    expect(await screen.findByRole('alert')).toHaveTextContent('too short to recognize reliably');
    expect(screen.getByRole('button', { name: 'Train' })).toBeDisabled();
  });

  it('tells the user where their recordings go before they record', async () => {
    backend();
    render(<WakeWordSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Create' }));
    expect(screen.getByText(/deleted about a day after training/)).toBeInTheDocument();
  });

  it('picks a running job back up after Settings was closed, and says what it is doing', async () => {
    backend({
      '/v1/wake/jobs/current': () =>
        reply({ job: { job_id: '41', status: 'training', wake_word: 'zephyra' } }),
      '/v1/wake/jobs/41': () =>
        reply({ job_id: '41', status: 'training', progress: 62, wake_word: 'zephyra' }),
    });
    render(<WakeWordSection />);

    expect(await screen.findByDisplayValue('zephyra')).toBeInTheDocument();
    expect(await screen.findByText(/Training your wake word/)).toBeInTheDocument();
  });

  it('after a failure says the current word still works and shows no model to activate', async () => {
    backend({
      '/v1/wake/jobs/current': () =>
        reply({ job: { job_id: '41', status: 'training', wake_word: 'zephyra' } }),
      '/v1/wake/jobs/41': () =>
        reply({
          job_id: '41',
          status: 'failed',
          progress: 30,
          error: 'Training is down on our side right now. Your recordings are fine and your current wake word still works.',
          error_reason: 'service_outage',
        }),
    });
    render(<WakeWordSection />);

    expect(await screen.findByText(/your current wake word still works/)).toBeInTheDocument();
    expect(screen.getByText(/This one is on us/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText('Viola').length).toBeGreaterThan(0));
  });
});

describe('WakeWordSection one action makes a trained word active', () => {
  const COMPUTER = {
    model_id: 'computer-1',
    name: 'computer',
    is_default: false,
    is_active: false,
    is_supported: true,
    architecture: 'temporal_cnn',
  };

  beforeEach(() => {
    authFetch.mockReset();
  });

  function trainedBackend() {
    let active = false;
    const activations = [];
    backend({
      '/v1/wake/models': () => reply({
        models: [{ ...VIOLA, is_active: !active }, { ...COMPUTER, is_active: active }],
        active_model_path: active ? 'computer.onnx' : 'viola.onnx',
      }),
      '/v1/wake/jobs/current': () => reply({ job: { job_id: '7', status: 'training', wake_word: 'computer' } }),
      '/v1/wake/jobs/7': () => reply({ job_id: '7', status: 'done', model_id: 'computer-1', model_path: 'computer.onnx' }),
      '/v1/wake/activate': () => {
        active = true;
        activations.push('computer-1');
        return reply({ model: { model_id: 'computer-1' }, reload: { reloaded: true } });
      },
    });
    return activations;
  }

  it('names the finished-training action and the list action the same, with no Switch or Activate', async () => {
    trainedBackend();
    render(<WakeWordSection />);

    expect(await screen.findByText(/"computer" is ready/)).toBeInTheDocument();
    // One in the finished-training row, one on the model's own row: the same words.
    expect(screen.getAllByRole('button', { name: 'Use this word' })).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Switch' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'In use' })).toBeDisabled();
  });

  it('pressing it makes the word selected and closes the finished-training row', async () => {
    const activations = trainedBackend();
    render(<WakeWordSection />);
    await screen.findByText(/"computer" is ready/);

    fireEvent.click(screen.getAllByRole('button', { name: 'Use this word' })[1]);

    expect(await screen.findByText('Selected "computer" as your wake word.')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Selected wake word:/)).toHaveTextContent('computer'));
    await waitFor(() => expect(screen.queryByText(/"computer" is ready/)).not.toBeInTheDocument());
    expect(activations).toEqual(['computer-1']);
    expect(screen.queryByRole('button', { name: 'Use this word' })).toBeEnabled();
  });

  it('a burst of clicks switches the word once (the laptop log had 8 switches in 2 seconds)', async () => {
    let release;
    let calls = 0;
    backend({
      '/v1/wake/models': () => reply({ models: [VIOLA, COMPUTER], active_model_path: 'viola.onnx' }),
      '/v1/wake/activate': () => {
        calls += 1;
        return new Promise((resolve) => { release = () => resolve(reply({ reload: { reloaded: true } })); })
          .then((value) => value);
      },
    });
    render(<WakeWordSection />);
    const button = await screen.findByRole('button', { name: 'Use this word' });

    for (let i = 0; i < 8; i += 1) fireEvent.click(button);

    expect(calls).toBe(1);
    await waitFor(() => expect(button).toBeDisabled());
    release();
    await waitFor(() => expect(screen.getByText('Selected "computer" as your wake word.')).toBeInTheDocument());
  });

  it('after Settings is closed and reopened the trained word still offers the same action', async () => {
    backend({
      '/v1/wake/models': () => reply({ models: [VIOLA, COMPUTER], active_model_path: 'viola.onnx' }),
    });
    render(<WakeWordSection />);

    const row = await screen.findByRole('button', { name: 'Use this word' });
    expect(row).toBeEnabled();
    expect(screen.queryByText(/is ready/)).not.toBeInTheDocument();
  });
});
