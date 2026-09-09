⚠️ WEB UI - SECONDARY INTERFACE
=================================

This directory contains the WEB BROWSER interface for Viola.

IMPORTANT:
  This is the SECONDARY interface.
  The PRIMARY interface is Qt Native Desktop (ui/qt_native/)

When fixing bugs:
  1. Ask user which interface they're using
  2. If Qt (most users): Fix ui/qt_native/ instead!
  3. If Web (browser): Fix this directory

These are SEPARATE codebases:
  ❌ Fixing this web UI does NOT fix Qt UI
  ❌ They use different files and technologies
  ✅ Backend (gpt_handler.py, routing/) is shared

Files in this directory:
  - index.html - Main web page
  - app.js - JavaScript for chat/music
  - settings.js - Settings modal
  - styles.css - Web styling

Run web interface:
  python START_WEB_INTERFACE.py
  Access: http://localhost:8756

Test web interface:
  npx playwright test tests/e2e/web/ui_controls.spec.ts tests/e2e/web/golden_path.spec.ts
  (the Selenium suite this pointed at, tests/ui/test_web_ui_comprehensive.py,
  was retired under #2962 -- dead pre-pivot element ids with zero live
  coverage; the Playwright specs under tests/e2e/web/ are the current web UI
  test surface)

See UI_ARCHITECTURE_EXPLAINED.md for full details.

Base URL overrides:
  - Set window.__VIOLA_BASE_URL__ at runtime to redirect all backend calls.
  - Use ui/static/api.js::apiFetch(path, options) for every network request.
  - apiFetch automatically prefixes the base URL and dispatches connection events.

Connection gating:
  - The web UI blocks interactive controls until apiFetch('/health') succeeds.
  - A top banner appears when the backend is unreachable and clears on reconnect.
