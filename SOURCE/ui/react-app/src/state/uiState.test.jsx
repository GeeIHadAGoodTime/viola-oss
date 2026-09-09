import { describe, expect, it } from 'vitest';
import { render, screen } from '../test/test-utils';
import {
  setCommandPaletteOpen,
  useUiSelector,
  useUiStateDispatch,
} from './uiState';

function CommandPaletteProbe() {
  const open = useUiSelector((state) => state.commandPaletteOpen);
  const dispatch = useUiStateDispatch();
  return (
    <button type="button" onClick={() => dispatch(setCommandPaletteOpen(!open))}>
      {open ? 'open' : 'closed'}
    </button>
  );
}

describe('uiState', () => {
  it('shares shell state through provider-backed selector hooks', async () => {
    const { user } = render(<CommandPaletteProbe />);

    expect(screen.getByRole('button', { name: 'closed' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'closed' }));

    expect(screen.getByRole('button', { name: 'open' })).toBeInTheDocument();
  });
});
