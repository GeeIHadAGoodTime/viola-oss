/**
 * Which sign-in providers the auth service will actually accept.
 *
 * GoTrue publishes its own capability document at `GET /auth/v1/settings`
 * (`{"external": {"google": true, "apple": false, ...}}`), reachable
 * same-origin on both surfaces — the desktop relays it
 * (auth/desktop_gotrue_proxy.py) and the cloud serves it directly.
 *
 * We read it rather than hardcoding a provider list because a hardcoded list
 * goes stale in the one direction that hurts: "Continue with Apple" shipped
 * on the sign-in screen while production GoTrue answered
 * `400 Unsupported provider: provider is not enabled` to every press. Reading
 * the service's own answer means the button disappears while that is true and
 * comes back by itself the day Apple is turned on, with no code change and no
 * second place to remember to update.
 *
 * Fail CLOSED: if the document cannot be read, no provider button is offered.
 * A missing button sends the user to email and password, which works; a
 * button that cannot work sends them nowhere.
 */

const SETTINGS_PATH = '/auth/v1/settings';

/** Providers the sign-in screen knows how to render, in display order. */
export const SUPPORTED_PROVIDERS = ['google', 'apple'] as const;

export type SupportedProvider = (typeof SUPPORTED_PROVIDERS)[number];

type GoTrueSettings = { external?: Record<string, unknown> };

let cached: Promise<SupportedProvider[]> | null = null;

async function readEnabledProviders(): Promise<SupportedProvider[]> {
  try {
    const response = await fetch(SETTINGS_PATH, { credentials: 'same-origin' });
    if (!response.ok) return [];
    const settings = (await response.json()) as GoTrueSettings;
    const external = settings?.external;
    if (!external || typeof external !== 'object') return [];
    return SUPPORTED_PROVIDERS.filter((provider) => external[provider] === true);
  } catch {
    return [];
  }
}

/**
 * Enabled providers, fetched once per page. Cached because the sign-in screen
 * can mount several times in one session and this answer does not change
 * under a running auth service.
 */
export function enabledAuthProviders(): Promise<SupportedProvider[]> {
  if (!cached) cached = readEnabledProviders();
  return cached;
}

/** Test seam: drop the memoised answer. */
export function resetEnabledAuthProvidersCache(): void {
  cached = null;
}

export default { enabledAuthProviders, resetEnabledAuthProvidersCache, SUPPORTED_PROVIDERS };
