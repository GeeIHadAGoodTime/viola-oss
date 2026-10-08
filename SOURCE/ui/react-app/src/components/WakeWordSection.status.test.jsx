import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import WakeWordSection from './WakeWordSection';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../hooks/useViolaApi', () => ({ authFetch }));
vi.mock('../utils/featureSurface', () => ({ isFeatureHidden: () => false }));

const viola = { model_id: 'default', name: 'Viola', is_default: true, is_active: true };
const athena = { model_id: 'athena', name: 'Athena', is_active: false };
const response = (data) => ({ ok: true, json: async () => ({ ok: true, data }) });

beforeEach(() => { authFetch.mockReset(); });

describe('wake model selection is not detector liveness', () => {
  it('does not invent a selected model before loading or after a failed request', async () => {
    let reject;
    authFetch.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    render(<WakeWordSection />);
    expect(screen.queryByText(/Currently listening|Now listening/)).not.toBeInTheDocument();
    expect(screen.getByText('Loading wake word selection…')).toBeInTheDocument();
    await act(async () => { reject(new Error('Model list unavailable')); });
    expect(screen.getByText('Wake word selection unavailable')).toBeInTheDocument();
    expect(screen.queryByText('Viola')).not.toBeInTheDocument();
  });

  it('labels configured models as selected without making a microphone claim', async () => {
    authFetch.mockResolvedValue(response({ models: [viola, athena] }));
    render(<WakeWordSection />);
    expect(await screen.findByText('Selected')).toBeInTheDocument();
    expect(screen.getByText(/Selected wake word:/)).toHaveTextContent('Selected wake word: Viola');
    expect(screen.queryByText(/Currently listening|Now listening/)).not.toBeInTheDocument();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
  });

  it('does not guess the first model is selected when no model is marked active', async () => {
    authFetch.mockResolvedValue(response({ models: [{ ...viola, is_active: false }] }));
    render(<WakeWordSection />);
    expect(await screen.findByText('No wake word selected')).toBeInTheDocument();
  });

  it.each([false, true])('selection success is truthful when reload.reloaded=%s', async (reloaded) => {
    authFetch.mockResolvedValueOnce(response({ models: [viola, athena] }))
      .mockResolvedValueOnce(response({ reload: { reloaded, reason: 'detector_not_running' } }))
      .mockResolvedValueOnce(response({ models: [{ ...viola, is_active: false }, { ...athena, is_active: true }] }));
    render(<WakeWordSection />);
    await screen.findByText('Athena');
    fireEvent.click(screen.getAllByRole('button', { name: 'Switch' }).find((button) => !button.disabled));
    expect(await screen.findByText('Selected "Athena" as your wake word.')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Selected wake word:/)).toHaveTextContent('Athena'));
    expect(screen.queryByText(/Currently listening|Now listening/)).not.toBeInTheDocument();
  });
});


it('keeps the unavailable training wizard unreachable without acquiring a microphone', async () => {
  const getUserMedia = vi.fn();
  const descriptor = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices');
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
  try {
    authFetch.mockResolvedValue(response({ models: [viola] }));
    render(<WakeWordSection />);
    const unavailable = await screen.findByRole('button', { name: 'Unavailable' });
    expect(unavailable).toBeDisabled();
    fireEvent.click(unavailable);
    expect(screen.queryByRole('button', { name: 'Record Sample' })).not.toBeInTheDocument();
    expect(getUserMedia).not.toHaveBeenCalled();
  } finally {
    if (descriptor) Object.defineProperty(navigator, 'mediaDevices', descriptor);
    else delete navigator.mediaDevices;
  }
});
