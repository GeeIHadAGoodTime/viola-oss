import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import AccentPicker from './AccentPicker';
import { setAccent } from '../config';

vi.mock('../config', () => ({ setAccent: vi.fn() }));

beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); });

describe('AccentPicker theme readability', () => {
  it('uses theme-aware foreground and surface for the editable hex value', () => {
    render(<AccentPicker />);
    const input = screen.getByPlaceholderText('Paste any #hex');
    expect(input.style.color).toBe('var(--text-primary)');
    expect(input.style.background).toBe('var(--bg-surface)');
    expect(screen.getByText('BRONZES & COPPERS').style.color).toBe('var(--text-secondary)');
    expect(screen.getByText(/Auto-applies on paste/).style.color).toBe('var(--text-secondary)');
  });

  it('keeps valid short-hex changes and ignores invalid input', () => {
    const onChange = vi.fn();
    render(<AccentPicker onChange={onChange} />);
    const input = screen.getByPlaceholderText('Paste any #hex');
    fireEvent.change(input, { target: { value: '#abc' } });
    expect(setAccent).toHaveBeenCalledWith('#AABBCC');
    expect(onChange).toHaveBeenCalledWith('#AABBCC');
    fireEvent.change(input, { target: { value: 'invalid' } });
    expect(setAccent).toHaveBeenCalledTimes(1);
  });
});
