/** Backend-owned deployment metadata; this selects UI, never authorizes API access. */
let declaredSurface = null;

export function setBackendSurface(health) {
  declaredSurface = health?.app_surface === 'desktop' || health?.app_surface === 'cloud'
    ? health.app_surface
    : null;
}

export function isLocalDesktopBackend() {
  return declaredSurface === 'desktop';
}
