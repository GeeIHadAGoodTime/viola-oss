import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import HandsFreeMicIndicator from './HandsFreeMicIndicator';

describe('hands-free mic disclosure', () => {
  it('distinguishes initialization, detection, paused detection, failure and off', () => {
    const { rerender } = render(<HandsFreeMicIndicator status="loading" />);
    expect(screen.getByRole('status')).toHaveTextContent('starting');
    rerender(<HandsFreeMicIndicator status="listening" />);
    expect(screen.getByRole('status')).toHaveTextContent('mic is listening for "Viola"');
    rerender(<HandsFreeMicIndicator status="listening" paused />);
    expect(screen.getByRole('status')).toHaveTextContent('mic active; wake detection paused');
    expect(screen.getByRole('status')).not.toHaveTextContent('listening for');
    rerender(<HandsFreeMicIndicator status="error" error="Permission denied" />);
    expect(screen.getByRole('status')).toHaveTextContent('Hands-free unavailable: Permission denied');
    rerender(<HandsFreeMicIndicator status="off" />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
