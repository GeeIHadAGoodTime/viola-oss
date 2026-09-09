import { describe, expect, it } from 'vitest';
import { randomUUID } from 'node:crypto';
import { isYouTubeEmbedMessage, resolveYouTubeEmbedUrl, youtubeEmbedFrameUrl } from './youtubeEmbed';

describe('configured spoke helper', () => {
  it('preserves the hosted default', () => {
    expect(resolveYouTubeEmbedUrl()).toBe('https://useviola.com/embed');
  });

  it('uses a custom path, parent identity and muted autoplay without URL injection', () => {
    const url = new URL(youtubeEmbedFrameUrl('https://player.example.org/helper/embed.html', 'a&mute=0', 'http://192.168.1.10:8756'));
    expect(url.origin).toBe('https://player.example.org');
    expect(url.pathname).toBe('/helper/embed.html');
    expect(url.searchParams.get('v')).toBe('a&mute=0');
    expect(url.searchParams.get('mute')).toBe('1');
    expect(url.searchParams.get('autoplay')).toBe('1');
    expect(url.searchParams.get('parent_origin')).toBe('http://192.168.1.10:8756');
  });

  it.each(['javascript:alert(1)', 'data:text/html,hi', '//example.org/embed', 'http://example.org/embed',
    'https://example.org/embed#fragment', "https://example.org/;script-src *"])(
    'rejects unsafe or ambiguous helper endpoint %s', (value) => {
      expect(() => resolveYouTubeEmbedUrl(value)).toThrow();
    });

  it('rejects URL credentials', () => {
    const url = new URL('https://example.org/embed');
    url.username = randomUUID();
    url.password = randomUUID();
    expect(() => resolveYouTubeEmbedUrl(url.toString())).toThrow();
  });

  it.each(['http://localhost:8100/embed.html', 'http://127.0.0.1:8100/embed.html', 'http://[::1]:8100/embed.html'])(
    'permits loopback development endpoint %s', (value) => expect(resolveYouTubeEmbedUrl(value)).toBe(value));

  it('requires both the configured origin and the mounted player window', () => {
    const frame = {};
    const endpoint = 'https://player.example.org/embed.html';
    expect(isYouTubeEmbedMessage({ origin: 'https://player.example.org', source: frame }, frame, endpoint)).toBe(true);
    expect(isYouTubeEmbedMessage({ origin: 'https://useviola.com', source: frame }, frame, endpoint)).toBe(false);
    expect(isYouTubeEmbedMessage({ origin: 'https://player.example.org', source: {} }, frame, endpoint)).toBe(false);
    expect(isYouTubeEmbedMessage({ origin: 'null', source: frame }, frame, endpoint)).toBe(false);
    expect(isYouTubeEmbedMessage({ origin: 'https://player.example.org', source: null }, null, endpoint)).toBe(false);
  });
});
