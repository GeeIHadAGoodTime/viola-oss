import { GoTrueClient } from '@supabase/auth-js';
import { inMemorySessionStorage, purgeLegacyLocalStorageSessions } from './sessionStore';

type GoTrueClientOptions = ConstructorParameters<typeof GoTrueClient>[0];

export const DEFAULT_GOTRUE_URL = 'https://api.useviola.com/auth/v1';
export const GOTRUE_STORAGE_KEY = 'viola-gotrue-session';

const GOTRUE_AUTHORIZE_PATH = '/auth/v1/authorize';

// SEC-017: tokens live in memory, never localStorage (XSS can't scrape the heap
// the way it reads localStorage). Drop any token a prior build left on disk.
purgeLegacyLocalStorageSessions([GOTRUE_STORAGE_KEY]);

// Default to the CURRENT ORIGIN's /auth/v1, not a hardcoded cloud host. GoTrue is
// cloud-only, but both surfaces reach it same-origin: the desktop app proxies
// localhost:8756/auth/v1 -> cloud (auth/desktop_gotrue_proxy.py), and the cloud SPA
// serves api.useviola.com/auth/v1 directly. Hardcoding the cloud host made the
// desktop GoTrue client fire CROSS-ORIGIN from localhost -> CORS-blocked, which
// (alongside the same-origin authClient) was the dual-provider login blocker.
// VITE_GOTRUE_URL still overrides; DEFAULT_GOTRUE_URL is the non-browser/SSR fallback.
function _sameOriginGoTrueUrl(): string {
  if (typeof window !== 'undefined' && window.location && window.location.origin) {
    return window.location.origin + '/auth/v1';
  }
  return DEFAULT_GOTRUE_URL;
}

function readConfiguredGoTrueUrl(): string {
  const viteUrl = import.meta.env?.VITE_GOTRUE_URL;
  const processUrl = typeof process !== 'undefined'
    ? process.env?.VITE_GOTRUE_URL
    : undefined;
  return viteUrl || processUrl || _sameOriginGoTrueUrl();
}

export function normalizeGoTrueUrl(url: string): string {
  return String(url || DEFAULT_GOTRUE_URL).replace(/\/+$/, '');
}

function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, '');
  return (
    host === 'localhost'
    || host.endsWith('.localhost')
    || host === '::1'
    || host === '0:0:0:0:0:0:0:1'
    || /^127(?:\.\d{1,3}){3}$/.test(host)
  );
}

function isGoTrueAuthorizePath(pathname: string): boolean {
  return pathname.replace(/\/+$/, '') === GOTRUE_AUTHORIZE_PATH;
}

export function externalOAuthAuthorizeUrl(authorizeUrl: string): string {
  const parsed = new URL(authorizeUrl);
  if (!isGoTrueAuthorizePath(parsed.pathname) || !isLoopbackHost(parsed.hostname)) {
    return authorizeUrl;
  }

  const publicAuthorizeUrl = new URL(`${normalizeGoTrueUrl(DEFAULT_GOTRUE_URL)}/authorize`);
  publicAuthorizeUrl.search = parsed.search;
  publicAuthorizeUrl.hash = parsed.hash;
  return publicAuthorizeUrl.toString();
}

export function createGoTrueClient(options: Partial<GoTrueClientOptions> = {}) {
  const url = normalizeGoTrueUrl(options.url || readConfiguredGoTrueUrl());
  const { url: _url, ...clientOptions } = options;

  return new GoTrueClient({
    url,
    storageKey: GOTRUE_STORAGE_KEY,
    // SEC-017: in-memory storage adapter keeps access+refresh tokens out of
    // localStorage so an XSS can't read them off disk. persistSession stays
    // true so the live tab keeps the session + auto-refresh; it just persists
    // into the heap, not localStorage. Callers may override via clientOptions
    // (e.g. tests).
    storage: inMemorySessionStorage,
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: true,
    flowType: 'pkce',
    ...clientOptions,
  });
}

export const gotrueClient = createGoTrueClient();
export type GoTrueAuthClient = typeof gotrueClient;

export async function getGoTrueAccessToken(
  client: GoTrueAuthClient = gotrueClient,
): Promise<string | null> {
  const { data, error } = await client.getSession();
  if (error) return null;
  return data.session?.access_token || null;
}
