/** One endpoint determines iframe navigation and both message-origin checks. */
export const DEFAULT_YOUTUBE_EMBED_URL = 'https://useviola.com/embed';

export function resolveYouTubeEmbedUrl(value = DEFAULT_YOUTUBE_EMBED_URL) {
  if (/[\s'";<>\\]/.test(value || '')) {
    throw new Error('VITE_YOUTUBE_EMBED_URL contains invalid URL characters');
  }
  const url = new URL(value || DEFAULT_YOUTUBE_EMBED_URL);
  const localHttp = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((!localHttp && url.protocol !== 'https:') || url.username || url.password || url.hash) {
    throw new Error('VITE_YOUTUBE_EMBED_URL must be an HTTPS URL without credentials or a fragment (HTTP is allowed on loopback for development).');
  }
  return url.href;
}

export function youtubeEmbedFrameUrl(endpoint, videoId, parentOrigin) {
  const url = new URL(resolveYouTubeEmbedUrl(endpoint));
  url.searchParams.set('v', videoId);
  url.searchParams.set('autoplay', '1');
  url.searchParams.set('mute', '1');
  url.searchParams.set('playsinline', '1');
  url.searchParams.set('parent_origin', parentOrigin);
  return url.href;
}

export function isYouTubeEmbedMessage(event, iframeWindow, endpoint) {
  return Boolean(iframeWindow) && event.source === iframeWindow && event.origin === new URL(endpoint).origin;
}
