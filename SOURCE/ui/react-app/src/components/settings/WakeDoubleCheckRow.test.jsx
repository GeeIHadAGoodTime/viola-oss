/**
 * Stage 2 had no switch in Voice settings (1.0.5 build-9 audit, T6): the setting
 * existed and the brief described it, but nobody could reach it.
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '../../test/test-utils';
import WakeDoubleCheckRow from './WakeDoubleCheckRow';

const toggle = () => screen.getByRole('switch', { name: 'Double-check the wake word' });

describe('Double-check the wake word', () => {
  it('reads as on when the setting was never saved', () => {
    render(<WakeDoubleCheckRow enabled={undefined} onChange={() => {}} />);
    expect(toggle()).toHaveAttribute('aria-checked', 'true');
  });

  it('saves false when turned off', () => {
    const onChange = vi.fn();
    render(<WakeDoubleCheckRow enabled onChange={onChange} />);
    fireEvent.click(toggle());
    expect(onChange).toHaveBeenCalledWith(false);
  });
});
