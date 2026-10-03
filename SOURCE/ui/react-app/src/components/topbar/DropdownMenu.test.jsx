import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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


beforeEach(() => vi.clearAllMocks());

describe('DropdownMenu keyboard lifecycle', () => {
  it('focuses the first item when opened and returns focus to its opener on Escape', () => {
    const { rerender } = render(<><button>Menu opener</button><DropdownMenu isOpen={false} {...handlers} /></>);
    const opener = screen.getByRole('button', { name: 'Menu opener' });
    opener.focus();
    rerender(<><button>Menu opener</button><DropdownMenu isOpen {...handlers} /></>);
    expect(screen.getByRole('menuitem', { name: 'Chat History' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement, { key: 'Escape' });
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    expect(opener).toHaveFocus();
  });
  it('dismisses on Escape even when focus has stayed outside the menu', () => {
    render(<DropdownMenu isOpen {...handlers} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
  });
  it('does not consume Escape after closing or unmounting', () => {
    const { rerender, unmount } = render(<DropdownMenu isOpen {...handlers} />);
    rerender(<DropdownMenu isOpen={false} {...handlers} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(handlers.onClose).not.toHaveBeenCalled();
    rerender(<DropdownMenu isOpen {...handlers} />);
    unmount();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(handlers.onClose).not.toHaveBeenCalled();
  });
  it('keeps arrow navigation and Tab dismissal without selecting an item', () => {
    render(<DropdownMenu isOpen {...handlers} />);
    const first = screen.getByRole('menuitem', { name: 'Chat History' });
    first.focus();
    fireEvent.keyDown(first, { key: 'ArrowUp' });
    const last = screen.getByRole('menuitem', { name: 'Help & Guide' });
    expect(last).toHaveFocus();
    fireEvent.keyDown(last, { key: 'ArrowDown' });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: 'Tab' });
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    expect(handlers.onOpenHistory).not.toHaveBeenCalled();
  });
});
