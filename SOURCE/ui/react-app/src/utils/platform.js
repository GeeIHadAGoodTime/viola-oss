// Host-OS detection for the Viola UI.
//
// The same React app ships inside the desktop Qt webview on every OS (Windows,
// macOS, Linux) and in the browser funnel. Platform-specific copy — most
// notably the login-item / autostart label — must read correctly on each OS.
// The webview's `navigator` reflects the host OS, so we derive the platform
// from it. This is display-only copy selection; it never gates behavior.

export function getPlatform() {
  if (typeof navigator === 'undefined') return 'unknown';
  const hint = `${navigator.userAgent || ''} ${navigator.platform || ''}`;
  if (/Mac|iPhone|iPad|iPod/i.test(hint)) return 'mac';
  if (/Win/i.test(hint)) return 'windows';
  if (/Linux|X11|CrOS/i.test(hint)) return 'linux';
  return 'unknown';
}

// Platform-appropriate label + description for the "launch at startup" toggle.
// macOS calls this a login item ("Start at Login", matching System Settings ->
// General -> Login Items); Windows starts apps at boot; Linux at session login.
export function getStartupToggleCopy() {
  switch (getPlatform()) {
    case 'mac':
      return {
        title: 'Start at Login',
        description: 'Open Viola automatically when you log in',
      };
    case 'linux':
      return {
        title: 'Start at Login',
        description: 'Launch Viola when you log in',
      };
    case 'windows':
      return {
        title: 'Start on Windows Boot',
        description: 'Launch Viola when your computer starts',
      };
    default:
      // Unknown host (e.g. an unrecognized webview): use OS-neutral copy rather
      // than naming the wrong platform.
      return {
        title: 'Start at Startup',
        description: 'Launch Viola when your computer starts',
      };
  }
}

// Platform-appropriate remediation copy for a denied/unconfirmed microphone
// permission during onboarding's mic-check step. This flow is desktop-only
// (see useVoiceOnboarding.js), so getPlatform() reflects the real host OS,
// not an arbitrary browser tab — the OS-level privacy setting is genuinely
// the right advice on both Windows and macOS. Linux mic permission handling
// varies by desktop environment (GNOME, KDE, etc.), so the copy there stays
// generic rather than naming a specific settings path that may not exist.
export function getMicPermissionGuidance() {
  switch (getPlatform()) {
    case 'mac':
      return 'Microphone access could not be confirmed. Check System Settings → Privacy & Security → Microphone to allow Viola, then try again.';
    case 'windows':
      return 'Microphone access could not be confirmed. Check Windows microphone privacy settings and try again.';
    case 'linux':
      return 'Microphone access could not be confirmed. Check your microphone privacy settings and try again.';
    default:
      // Unknown host: stay OS-neutral rather than naming the wrong platform.
      return 'Microphone access could not be confirmed. Check your system’s microphone privacy settings and try again.';
  }
}
