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
