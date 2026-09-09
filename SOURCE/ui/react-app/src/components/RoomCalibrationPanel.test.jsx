import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import RoomCalibrationPanel from './RoomCalibrationPanel';

// #3003 pickup check: this panel's range input was never part of the
// snap-back half of the bug (its value is local, and nothing polls over it),
// but it did carry the other half — a PUT per input tick on a slider with 400
// steps, so one drag across the range meant a burst of writes to
// /api/v1/multiroom/<room>/calibration. The slider still moves per tick; only
// the write settles.

const apiFetchMock = vi.hoisted(() => vi.fn());

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: apiFetchMock,
}));

const puts = () => apiFetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');

async function renderPanel(offsetMs = 0) {
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation((path, init) => {
    if (init?.method === 'PUT') return Promise.resolve({ ok: true });
    return Promise.resolve({ offset_ms: offsetMs });
  });
  render(<RoomCalibrationPanel roomId="kitchen" />);
  return screen.findByRole('slider', { name: 'Audio sync offset' });
}

describe('RoomCalibrationPanel sync offset slider (#3003)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('collapses a drag into one calibration write instead of one per tick', async () => {
    const slider = await renderPanel(0);

    for (const tick of [-20, -45, -70, -95, -120, -150, -180]) {
      fireEvent.change(slider, { target: { value: String(tick) } });
    }

    // The thumb and the readout follow every tick...
    expect(slider).toHaveValue('-180');
    expect(screen.getByText('-180ms')).toBeInTheDocument();
    // ...but nothing has gone out yet.
    expect(puts()).toHaveLength(0);

    fireEvent.pointerUp(slider);

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(JSON.parse(puts()[0][1].body)).toEqual({ offset_ms: -180 });
  });

  it('still writes the settled value when the drag just stops', async () => {
    const slider = await renderPanel(0);

    fireEvent.change(slider, { target: { value: '60' } });
    expect(puts()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(50);

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(JSON.parse(puts()[0][1].body)).toEqual({ offset_ms: 60 });
  });

  it('resets to zero in one write', async () => {
    const slider = await renderPanel(120);
    expect(slider).toHaveValue('120');

    fireEvent.click(screen.getByRole('button', { name: 'Reset to 0' }));

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(JSON.parse(puts()[0][1].body)).toEqual({ offset_ms: 0 });
    expect(slider).toHaveValue('0');
    expect(screen.getByText('0ms')).toBeInTheDocument();
  });
});
