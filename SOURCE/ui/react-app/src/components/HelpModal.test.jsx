import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import HelpModal from './HelpModal';
function openTroubleshooting() {
  render(<HelpModal isOpen onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Troubleshooting' }));
}
describe('HelpModal current desktop guidance', () => {
  it('points wake, music, calendar and audio troubleshooting at existing Settings tabs', () => {
    openTroubleshooting();
    for (const name of [/Viola isn't responding/, /Music won't play/, /Calendar \/ email/, /Audio output is wrong/, /Phone calls fail/]) {
      fireEvent.click(screen.getByRole('button', { name }));
    }
    expect(screen.getByText(/Adjust wake sensitivity/)).toHaveTextContent('Settings → Voice');
    expect(screen.getByText(/Verify your music provider/)).toHaveTextContent('Settings → Music');
    expect(screen.getByText(/Connect your calendar provider/)).toHaveTextContent('Settings → Connections → Calendar');
    expect(screen.getByText(/Set the output device/)).toHaveTextContent('Settings → System → Audio Devices');
    expect(screen.getByText(/For recording/)).toHaveTextContent('Settings → Account → Phone');
    expect(document.body).not.toHaveTextContent('Settings → Preferences');
    expect(document.body).not.toHaveTextContent('Settings → Services');
    expect(document.body).not.toHaveTextContent('Settings → Music & Voice');
  });
  it('explains unavailable training while preserving installed wake-word selection', () => {
    openTroubleshooting();
    fireEvent.click(screen.getByRole('button', { name: /Custom wake-word training/ }));
    expect(screen.getByText(/Custom wake-word training is currently unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Existing local models/)).toHaveTextContent('Settings → Customize');
    expect(document.body).not.toHaveTextContent('try training again');
  });
});
