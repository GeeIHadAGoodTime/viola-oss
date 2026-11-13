import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { confirmDialog, dismissAllDialogs, promptDialog } from './ViolaDialog';

afterEach(async () => {
  await act(async () => { dismissAllDialogs(); });
});

// Wrapped in an object: an async function returning the promise itself would wait for the answer.
async function open(ask) {
  let answer;
  await act(async () => { answer = ask(); });
  return { answer };
}

describe('Viola dialogs', () => {
  it('resolves true on confirm and false on cancel', async () => {
    const { answer: yes } = await open(() => confirmDialog({ title: 'Delete "Trip"?', confirmLabel: 'Delete', danger: true }));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Delete "Trip"?');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete' })); });
    await expect(yes).resolves.toBe(true);

    const { answer: no } = await open(() => confirmDialog({ title: 'Delete "Trip"?', confirmLabel: 'Delete', danger: true }));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
    await expect(no).resolves.toBe(false);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  // 1.0.5 build 10: Ctrl+A in the Rename box highlighted the whole window.
  // With focus outside the dialog, Ctrl+A now selects the field's text only.
  it('keeps Ctrl+A in the rename field when focus has left the dialog', async () => {
    const { answer } = await open(() => promptDialog({ title: 'Rename chat', label: 'Chat name', defaultValue: 'Trip plans' }));
    const field = screen.getByRole('textbox', { name: 'Chat name' });
    await act(async () => { field.blur(); document.body.focus(); });
    expect(field).not.toHaveFocus();
    const event = new KeyboardEvent('keydown', { key: 'a', ctrlKey: true, bubbles: true, cancelable: true });
    await act(async () => { document.body.dispatchEvent(event); });
    expect(event.defaultPrevented).toBe(true);
    expect(field).toHaveFocus();
    expect([field.selectionStart, field.selectionEnd]).toEqual([0, 'Trip plans'.length]);
    await act(async () => { fireEvent.keyDown(field, { key: 'Escape' }); });
    await expect(answer).resolves.toBe(null);
  });

  it('starts a destructive confirm on Cancel', async () => {
    await open(() => confirmDialog({ title: 'Delete?', confirmLabel: 'Delete', danger: true }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus());
  });

  it('takes Escape for itself: it never reaches the page underneath', async () => {
    const underneath = vi.fn();
    document.addEventListener('keydown', underneath);
    const { answer } = await open(() => confirmDialog({ title: 'Reset?' }));
    await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }); });
    await expect(answer).resolves.toBe(false);
    expect(underneath).not.toHaveBeenCalled();
    document.removeEventListener('keydown', underneath);
  });

  it('returns the edited text from a prompt, and null when cancelled', async () => {
    const { answer: edited } = await open(() => promptDialog({ title: 'Rename chat', label: 'Chat name', defaultValue: 'Old' }));
    const field = screen.getByLabelText('Chat name');
    expect(field).toHaveValue('Old');
    fireEvent.change(field, { target: { value: 'New name' } });
    await act(async () => { fireEvent.keyDown(field, { key: 'Enter' }); });
    await expect(edited).resolves.toBe('New name');

    const { answer: cancelled } = await open(() => promptDialog({ title: 'Rename chat', defaultValue: 'Old' }));
    await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }); });
    await expect(cancelled).resolves.toBeNull();
  });

  it('shows a second request after the first is answered', async () => {
    const { answer: first } = await open(() => confirmDialog({ title: 'First' }));
    const { answer: second } = await open(() => confirmDialog({ title: 'Second' }));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('First');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'OK' })); });
    await expect(first).resolves.toBe(true);
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Second');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
    await expect(second).resolves.toBe(false);
  });
});
