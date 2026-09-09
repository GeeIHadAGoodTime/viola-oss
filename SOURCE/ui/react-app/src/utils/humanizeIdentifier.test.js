import { describe, it, expect } from 'vitest';
import { humanizeIdentifier } from './humanizeIdentifier';

describe('humanizeIdentifier', () => {
  it('breaks snake_case into a readable phrase', () => {
    expect(humanizeIdentifier('fill_payment_details')).toBe('Fill payment details');
    expect(humanizeIdentifier('set_volume')).toBe('Set volume');
    expect(humanizeIdentifier('answer')).toBe('Answer');
  });

  it('handles kebab-case, dots, colons and slashes', () => {
    expect(humanizeIdentifier('web-search')).toBe('Web search');
    expect(humanizeIdentifier('calendar.list_events')).toBe('Calendar list events');
    expect(humanizeIdentifier('mcp::open_page')).toBe('MCP open page');
    expect(humanizeIdentifier('browser/navigate')).toBe('Browser navigate');
  });

  it('splits camelCase and PascalCase', () => {
    expect(humanizeIdentifier('fillPaymentDetails')).toBe('Fill payment details');
    expect(humanizeIdentifier('OpenBrowser')).toBe('Open browser');
  });

  it('keeps acronyms upper case wherever they land', () => {
    expect(humanizeIdentifier('fetch_url')).toBe('Fetch URL');
    expect(humanizeIdentifier('url_fetch')).toBe('URL fetch');
    expect(humanizeIdentifier('read_pdf_text')).toBe('Read PDF text');
  });

  it('returns empty string for anything unusable, so callers can fall back', () => {
    expect(humanizeIdentifier('')).toBe('');
    expect(humanizeIdentifier('   ')).toBe('');
    expect(humanizeIdentifier('___')).toBe('');
    expect(humanizeIdentifier(null)).toBe('');
    expect(humanizeIdentifier(undefined)).toBe('');
    expect(humanizeIdentifier(42)).toBe('');
  });

  it('never returns a string still carrying identifier separators', () => {
    const slugs = ['a_b', 'a-b', 'a.b', 'a::b', 'a/b', 'aB'];
    for (const slug of slugs) {
      expect(humanizeIdentifier(slug)).not.toMatch(/[_\-.:/\\]/);
    }
  });
});
