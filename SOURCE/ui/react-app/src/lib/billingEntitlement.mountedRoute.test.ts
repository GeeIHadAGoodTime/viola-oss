import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readBillingSubscription } from './billingEntitlement';

// The desktop Account page reads its plan from the LOCAL backend origin. If the
// backend never mounts that path, every signed-in user sees "Plan unavailable"
// (1.0.5). Tie the UI's request path to the route the desktop actually mounts.
const desktopApp = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', 'backend', 'fastapi_app.py'), 'utf8',
);

function desktopBillingReadRoutes(): Set<string> {
  const start = desktopApp.indexOf('def _ensure_billing_usage_routes(');
  expect(start).toBeGreaterThan(-1);
  const end = desktopApp.indexOf('\ndef ', start + 1);
  const body = desktopApp.slice(start, end);
  return new Set([...body.matchAll(/add_api_route\(\s*"([^"]+)"[^)]*methods=\["GET"\]/g)].map((m) => m[1]));
}

const session = { access_token: 'existing-access', user: { id: 'user-a' } } as any;
const desktopOrigin = 'http://127.0.0.1:8765';

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as any).__VIOLA_BASE_URL__;
});

describe('desktop plan read reaches a mounted route', () => {
  it('requests a path the desktop backend mounts, on the desktop origin', async () => {
    (window as any).__VIOLA_BASE_URL__ = desktopOrigin;
    // The desktop forwards the cloud body verbatim (no envelope).
    const fetch = vi.fn(async () => ({ ok: true, json: async () => ({
      user_id: 'user-a', plan_id: 'pro_monthly', plan_family: 'pro', status: 'active',
      has_paid_access: true, subscription_source: 'stripe', current_period_end: null,
    }) }));
    vi.stubGlobal('fetch', fetch);

    const value = await readBillingSubscription(session);

    const requested = new URL(fetch.mock.calls[0][0] as string);
    expect(requested.origin).toBe(desktopOrigin);
    expect(desktopBillingReadRoutes()).toContain(requested.pathname);
    expect(value.plan_family).toBe('pro');
    expect(value.has_paid_access).toBe(true);
  });
});
