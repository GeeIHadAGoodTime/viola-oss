/**
 * Tests for utility functions: timeFormat and artProxy.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { formatTimeDisplay } from '../../utils/timeFormat';
import { getProxiedArtUrl } from '../../utils/artProxy';

describe('formatTimeDisplay', () => {
  // Use a fixed date to avoid timezone flakiness: 2026-01-15 14:30:00 UTC
  const fixedDate = new Date('2026-01-15T14:30:00Z');

  it('should format a Date object', () => {
    const result = formatTimeDisplay(fixedDate);
    // Exact format depends on locale, but it should be a non-empty string
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });

  it('should format a timestamp number', () => {
    const result = formatTimeDisplay(fixedDate.getTime());
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });

  it('should format a date string', () => {
    const result = formatTimeDisplay('2026-01-15T14:30:00Z');
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });

  it('should respect 12h format', () => {
    const result = formatTimeDisplay(fixedDate, '12h');
    // 12h format should contain AM or PM
    expect(result).toMatch(/AM|PM/i);
  });

  it('should respect 24h format', () => {
    const result = formatTimeDisplay(fixedDate, '24h');
    // 24h format should NOT contain AM/PM
    expect(result).not.toMatch(/AM|PM/i);
  });

  it('should default to auto format', () => {
    // auto uses browser locale — just verify it doesn't throw
    const result = formatTimeDisplay(fixedDate, 'auto');
    expect(typeof result).toBe('string');
  });
});

describe('getProxiedArtUrl', () => {
  it('should return null for falsy input', () => {
    expect(getProxiedArtUrl(null)).toBeNull();
    expect(getProxiedArtUrl(undefined)).toBeNull();
    expect(getProxiedArtUrl('')).toBeNull();
  });

  it('should pass through relative URLs unchanged', () => {
    expect(getProxiedArtUrl('/static/images/cover.jpg')).toBe('/static/images/cover.jpg');
  });

  it('should pass through data URLs unchanged', () => {
    const dataUrl = 'data:image/png;base64,iVBORw0KGgo=';
    expect(getProxiedArtUrl(dataUrl)).toBe(dataUrl);
  });

  it('should pass through blob URLs unchanged', () => {
    const blobUrl = 'blob:http://localhost/12345';
    expect(getProxiedArtUrl(blobUrl)).toBe(blobUrl);
  });

  it('should proxy remote URLs through the art-proxy endpoint', () => {
    const remoteUrl = 'https://cdn.example.com/art/cover.jpg';
    const result = getProxiedArtUrl(remoteUrl);
    expect(result).toBe(`/api/v1/art-proxy?url=${encodeURIComponent(remoteUrl)}`);
  });

  it('should encode special characters in the URL', () => {
    const url = 'https://example.com/art?size=300&format=jpg';
    const result = getProxiedArtUrl(url);
    expect(result).toContain(encodeURIComponent(url));
  });
});
