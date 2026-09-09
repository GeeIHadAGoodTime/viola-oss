/**
 * The two hand-rolled modal overlays in the SPA (this one and RoomGroupsModal)
 * were the only inset-0 backdrops that carried no dialog role -- everything
 * else routes through components/Modal.jsx, which has always had one. Without
 * it a screen reader reads the form as ordinary page content behind nothing,
 * and a [role=dialog] query finds no modal at all. Found while probing the live
 * cloud app (#3553).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '../../test/test-utils';
import RegisterMCPForm from './RegisterMCPForm';

describe('RegisterMCPForm accessibility', () => {
  it('is discoverable as a modal dialog', () => {
    render(<RegisterMCPForm isOpen onClose={vi.fn()} onSubmit={vi.fn()} />);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Register MCP Server');
  });
});
