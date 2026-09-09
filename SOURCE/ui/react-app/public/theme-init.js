// Early theme: set background before React loads to prevent flash
// of wrong color. Reads cached theme preference from localStorage.
//
// Externalized from index.html (was an inline <script>) so the cloud /app
// CSP can ship WITHOUT 'unsafe-inline' in script-src (#1063) -- a
// same-origin external file needs no CSP relaxation at all. Keep this file
// as the ONLY early-theme mechanism; do not re-inline it into index.html.
(function () {
  try {
    var mode = localStorage.getItem('viola_theme_mode');
    if (mode === 'light' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: light)').matches)) {
      var bg = '#f5f5f7';
      document.documentElement.style.backgroundColor = bg;
      // body not available yet, set via style tag override
      var s = document.createElement('style');
      s.textContent = 'html, body, #root { background: ' + bg + ' !important; }';
      document.head.appendChild(s);
    }
  } catch (e) {}
})();
