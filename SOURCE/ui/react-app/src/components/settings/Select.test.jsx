import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import Select, { shouldOpenUpward } from './Select';

const VOICES = [
  { value: 'af_heart', label: 'Heart' },
  { value: 'af_river', label: 'River' },
  { value: 'alloy', label: 'Alloy' },
  { value: 'nova', label: 'Nova' },
  { value: 'echo', label: 'Echo' },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderInScrollBox(triggerTop) {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function rect() {
    if (this.dataset?.testid === 'scroll-box') return { top: 0, bottom: 600, left: 0, right: 400, width: 400, height: 600 };
    if (this.getAttribute?.('aria-haspopup') === 'listbox') {
      return { top: triggerTop, bottom: triggerTop + 44, left: 0, right: 400, width: 400, height: 44 };
    }
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
  });
  render(
    <div data-testid="scroll-box" style={{ overflowY: 'auto', height: 600 }}>
      <Select label="Assistant Voice" value="af_heart" options={VOICES} onChange={() => {}} />
    </div>,
  );
  fireEvent.click(screen.getByRole('button', { name: /Assistant Voice/ }));
  return screen.getByRole('listbox');
}

describe('Select placement (1.0.5 build 7, X4.1: voice list clipped at the dialog bottom)', () => {
  it('opens upward when the list would be cut off below the dialog', () => {
    const listbox = renderInScrollBox(540);
    expect(listbox.style.bottom).toBe('calc(100% + 4px)');
    expect(listbox.style.top).toBe('');
  });

  it('opens downward when there is room below', () => {
    const listbox = renderInScrollBox(40);
    expect(listbox.style.top).toBe('calc(100% + 4px)');
    expect(listbox.style.bottom).toBe('');
  });

  it('decides from the room above and below the trigger', () => {
    const clip = { top: 0, bottom: 600 };
    expect(shouldOpenUpward({ top: 540, bottom: 584 }, clip, 8)).toBe(true);
    expect(shouldOpenUpward({ top: 40, bottom: 84 }, clip, 8)).toBe(false);
    // Short list that fits below: stays below even near the bottom.
    expect(shouldOpenUpward({ top: 470, bottom: 514 }, clip, 1)).toBe(false);
  });
});
