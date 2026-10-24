import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CallBriefing from './CallBriefing';

const briefing = {
  phone_number: '+1 555 0100',
  business_name: 'Testville Pizza',
  objective: 'Order a large margherita',
  talking_points: ['Pickup at 6pm'],
  approved: false,
};

describe('CallBriefing dismiss control', () => {
  it('is a named button that grows to a 44px target on touch', () => {
    // It was a bare "x" glyph: no accessible name and an 8x18px target.
    const onDismiss = vi.fn();
    render(<CallBriefing briefing={briefing} onDismiss={onDismiss} />);
    const button = screen.getByRole('button', { name: 'Dismiss call briefing' });
    expect(button.className).toContain('viola-touch-target');
    expect(button.style.padding).toBe('6px 8px');
    fireEvent.click(button);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
