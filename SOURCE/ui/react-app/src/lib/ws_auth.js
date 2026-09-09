import { authFetch } from '../hooks/useViolaApi';

export async function getWebSocketAuthToken() {
  const injectedToken = window.__VIOLA_WS_AUTH_TOKEN__ || '';
  if (injectedToken) {
    window.__VIOLA_WS_AUTH_TOKEN__ = '';
    return injectedToken;
  }

  try {
    const response = await authFetch('/v1/ws/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!response.ok) return '';
    const data = await response.json();
    return data?.data?.token || data?.token || '';
  } catch {
    return '';
  }
}
