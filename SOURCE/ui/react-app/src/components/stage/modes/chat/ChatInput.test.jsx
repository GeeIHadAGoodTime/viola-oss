/**
 * The chat composer had only a placeholder, so screen readers announced an
 * unlabeled multi-line text box: a placeholder is a hint that disappears once
 * the user types, not a name. Found alongside the Rooms modal's missing dialog
 * role while probing the live cloud app (#3553).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '../../../../test/test-utils';
import ChatInput from './ChatInput';

const renderInput = (props = {}) => render(
  <ChatInput
    value=""
    onChange={vi.fn()}
    onSend={vi.fn()}
    onStop={vi.fn()}
    onAttachFiles={vi.fn()}
    {...props}
  />,
);

describe('ChatInput accessibility', () => {
  it('names the composer for assistive tech, not only by placeholder', () => {
    renderInput();

    const composer = screen.getByRole('textbox', { name: 'Message Viola' });
    expect(composer.tagName).toBe('TEXTAREA');
  });
});
