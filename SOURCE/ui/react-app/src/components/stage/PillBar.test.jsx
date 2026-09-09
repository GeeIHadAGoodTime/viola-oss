import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../../test/test-utils';
import PillBar from './PillBar';

describe('PillBar', () => {
  it('announces phone and contextual status changes through a polite live region', () => {
    render(
      <PillBar
        activeMode="music"
        pinnedItems={[
          { id: 'music', label: 'Music', icon: 'music' },
          { id: 'phone', label: 'Phone', icon: 'phone', statusDot: true, meta: 'Live' },
        ]}
        contextualItems={[
          { id: 'agent', label: 'Checking page', icon: 'globe' },
        ]}
        onSelect={vi.fn()}
      />
    );

    expect(screen.getByTestId('stage-pill-status')).toHaveTextContent('Phone Live');
    expect(screen.getByTestId('stage-pill-status')).toHaveTextContent('Checking page available');
  });
});
