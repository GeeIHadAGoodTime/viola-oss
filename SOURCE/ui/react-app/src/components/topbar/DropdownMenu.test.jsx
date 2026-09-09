import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import DropdownMenu from './DropdownMenu';

const handlers = {
  onClose: vi.fn(),
  onOpenHistory: vi.fn(),
  onOpenQueue: vi.fn(),
  onOpenSettings: vi.fn(),
  onOpenRooms: vi.fn(),
  onOpenHelp: vi.fn(),
};

describe('DropdownMenu spoke surface', () => {
  it('shows the same navigation entries on spokes as the hub', () => {
    render(<DropdownMenu isOpen isSpoke {...handlers} />);

    expect(screen.getByRole('menuitem', { name: 'Chat History' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Queue' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Rooms' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Help & Guide' })).toBeInTheDocument();
  });
});
