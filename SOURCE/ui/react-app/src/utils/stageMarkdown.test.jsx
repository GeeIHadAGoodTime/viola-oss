/**
 * Stage replies printed the model's markdown literally (1.0.5 build 5):
 * `**12**`, `**Viola Pro**`, `[FIFA](https://...)`.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { renderStageMarkdown, stripStageMarkdown, shortLinkLabel } from './stageMarkdown';

function Reply({ text }) {
  return <p data-testid="reply">{renderStageMarkdown(text)}</p>;
}

describe('renderStageMarkdown', () => {
  it('turns bold into bold text with no asterisks', () => {
    render(<Reply text="15% of 80 is **12**. You're on the **Viola Pro** plan." />);
    const reply = screen.getByTestId('reply');
    expect(reply.textContent).toBe("15% of 80 is 12. You're on the Viola Pro plan.");
    expect(reply.querySelectorAll('strong')).toHaveLength(2);
  });

  it('shows a markdown link as its label, tappable, not as a raw URL', () => {
    render(<Reply text="The schedule is on [FIFA](https://www.fifa.com/en/tournaments/mens/worldcup)." />);
    const reply = screen.getByTestId('reply');
    expect(reply.textContent).toBe('The schedule is on FIFA.');
    const link = screen.getByRole('link', { name: 'FIFA' });
    expect(link).toHaveAttribute('href', 'https://www.fifa.com/en/tournaments/mens/worldcup');
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('shortens a bare URL to its host and keeps sentence punctuation outside', () => {
    render(<Reply text="See https://www.bbc.com/news/world-12345." />);
    expect(screen.getByTestId('reply').textContent).toBe('See bbc.com.');
    expect(screen.getByRole('link', { name: 'bbc.com' })).toHaveAttribute('href', 'https://www.bbc.com/news/world-12345');
  });

  it('never makes a non-http link clickable', () => {
    render(<Reply text="[click](javascript:alert(1)) here" />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByTestId('reply').textContent).not.toContain('](');
  });

  it('drops heading and list markers and keeps plain text untouched', () => {
    render(<Reply text={'## Weather\n- High 55\n- Low 41'} />);
    expect(screen.getByTestId('reply').textContent).toBe('Weather\n• High 55\n• Low 41');
    expect(renderStageMarkdown('plain words, file_name_here, 5 * 3 = 15')).toEqual(['plain words, file_name_here, 5 * 3 = 15']);
  });
});

describe('stripStageMarkdown / shortLinkLabel', () => {
  it('returns plain text', () => {
    expect(stripStageMarkdown("You're on the **Viola Pro** plan, see [FIFA](https://fifa.com).")).toBe(
      "You're on the Viola Pro plan, see FIFA.",
    );
    expect(shortLinkLabel('https://www.example.org/a/b')).toBe('example.org');
  });
});
