import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '../../test/test-utils';
import CommandPalette from './CommandPalette';

function PaletteHarness({ commands }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open palette</button>
      <CommandPalette
        open={open}
        commands={commands}
        onClose={() => setOpen(false)}
      />
    </>
  );
}

describe('CommandPalette', () => {
  it('closes with Escape and restores focus to the opener', async () => {
    const { user } = render(
      <PaletteHarness
        commands={[{ id: 'one', label: 'First command', perform: vi.fn() }]}
      />
    );
    const opener = screen.getByRole('button', { name: /open palette/i });

    await user.click(opener);
    const input = await screen.findByRole('combobox', { name: /search commands/i });
    await waitFor(() => expect(input).toHaveFocus());

    await user.keyboard('{Escape}');

    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('tracks the active option and runs the selected command with Enter', async () => {
    const first = vi.fn();
    const second = vi.fn();
    const { user } = render(
      <PaletteHarness
        commands={[
          { id: 'first', label: 'First command', perform: first },
          { id: 'second', label: 'Second command', perform: second },
        ]}
      />
    );

    await user.click(screen.getByRole('button', { name: /open palette/i }));
    const input = await screen.findByRole('combobox', { name: /search commands/i });
    await waitFor(() => expect(input).toHaveFocus());

    await user.keyboard('{ArrowDown}');
    const activeId = input.getAttribute('aria-activedescendant');
    expect(activeId).toBeTruthy();
    expect(document.getElementById(activeId)).toHaveTextContent('Second command');

    await user.keyboard('{Enter}');

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument();
  });

  it('keeps Tab focus inside the modal palette', async () => {
    const { user } = render(
      <PaletteHarness
        commands={[
          { id: 'first', label: 'First command', perform: vi.fn() },
          { id: 'second', label: 'Second command', perform: vi.fn() },
        ]}
      />
    );

    await user.click(screen.getByRole('button', { name: /open palette/i }));
    const input = await screen.findByRole('combobox', { name: /search commands/i });
    await waitFor(() => expect(input).toHaveFocus());

    await user.tab({ shift: true });
    expect(screen.getByRole('option', { name: /second command/i })).toHaveFocus();

    await user.tab();
    expect(input).toHaveFocus();
  });
});
