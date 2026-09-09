/**
 * Get a CORS-proxied URL for album art.
 * Routes remote image URLs through the hub's art-proxy endpoint to avoid
 * CORS issues on spoke browsers that load art from third-party CDNs.
 *
 * @param {string} originalUrl - The remote image URL
 * @returns {string|null} Proxied URL or null if input is falsy
 */
export function getProxiedArtUrl(originalUrl) {
  if (!originalUrl) return null;

  // Already a local/relative URL -- no proxy needed
  if (originalUrl.startsWith('/') || originalUrl.startsWith(window.location.origin)) {
    return originalUrl;
  }

  // Data URLs -- no proxy needed
  if (originalUrl.startsWith('data:')) {
    return originalUrl;
  }

  // Blob URLs -- no proxy needed
  if (originalUrl.startsWith('blob:')) {
    return originalUrl;
  }

  return `/api/v1/art-proxy?url=${encodeURIComponent(originalUrl)}`;
}
