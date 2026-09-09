/**
 * Vitest test environment setup for Viola React app.
 *
 * Configures global mocks for browser APIs that don't exist in jsdom:
 * - WebSocket (used by useWebSocket hook)
 * - fetch (used by useViolaApi and apiFetch)
 * - Notification (used by utils/notifications.js)
 * - localStorage (basic coverage — jsdom provides it but may need reset)
 * - import.meta.env stubs
 */
import '@testing-library/jest-dom';

// ---------------------------------------------------------------------------
// localStorage / sessionStorage
// Node >= 22 defines its own `localStorage` getter on globalThis which
// shadows jsdom's and evaluates to `undefined` unless node is started with
// --localstorage-file. Since vitest's jsdom env aliases window === globalThis,
// both bare `localStorage` and `window.localStorage` break. Install an
// in-memory Storage so tests behave the same on every Node version.
// ---------------------------------------------------------------------------
class MemoryStorage {
  constructor() { this._data = new Map(); }
  get length() { return this._data.size; }
  key(i) { return [...this._data.keys()][i] ?? null; }
  getItem(k) { return this._data.has(String(k)) ? this._data.get(String(k)) : null; }
  setItem(k, v) { this._data.set(String(k), String(v)); }
  removeItem(k) { this._data.delete(String(k)); }
  clear() { this._data.clear(); }
}
for (const name of ['localStorage', 'sessionStorage']) {
  if (!globalThis[name] || typeof globalThis[name].clear !== 'function') {
    Object.defineProperty(globalThis, name, {
      value: new MemoryStorage(),
      writable: true,
      configurable: true,
    });
  }
}

// ---------------------------------------------------------------------------
// Mock WebSocket
// ---------------------------------------------------------------------------
class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.OPEN;
    this.binaryType = 'blob';
    this._listeners = {};
  }

  send(data) {
    // no-op by default; tests can spy on this
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
  }

  addEventListener(event, handler) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(handler);
  }

  removeEventListener(event, handler) {
    if (!this._listeners[event]) return;
    this._listeners[event] = this._listeners[event].filter(h => h !== handler);
  }

  // Helper for tests to simulate events
  _emit(event, data) {
    (this._listeners[event] || []).forEach(h => h(data));
    const prop = `on${event}`;
    if (typeof this[prop] === 'function') this[prop](data);
  }
}

// Attach static constants to instances via prototype too
MockWebSocket.prototype.CONNECTING = 0;
MockWebSocket.prototype.OPEN = 1;
MockWebSocket.prototype.CLOSING = 2;
MockWebSocket.prototype.CLOSED = 3;

global.WebSocket = MockWebSocket;

// ---------------------------------------------------------------------------
// Mock fetch
// ---------------------------------------------------------------------------
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve(''),
  })
);

// ---------------------------------------------------------------------------
// Mock Notification API
// ---------------------------------------------------------------------------
global.Notification = class MockNotification {
  static permission = 'granted';
  static requestPermission = vi.fn(() => Promise.resolve('granted'));
  constructor(title, options) {
    this.title = title;
    this.options = options;
  }
};

// ---------------------------------------------------------------------------
// Mock ResizeObserver (used by SmartDisplay for media area bounds)
// ---------------------------------------------------------------------------
global.ResizeObserver = class MockResizeObserver {
  constructor(callback) {
    this._callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
};

// ---------------------------------------------------------------------------
// Mock matchMedia (used by theme system)
// ---------------------------------------------------------------------------
global.matchMedia = vi.fn((query) => ({
  matches: query === '(prefers-color-scheme: dark)',
  media: query,
  onchange: null,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Mock window properties used by config.js
// ---------------------------------------------------------------------------
if (typeof window !== 'undefined') {
  window.__VIOLA_BASE_URL__ = '';
  window.__VIOLA_API_KEY__ = 'test-api-key';  // pragma: allowlist secret
}

// ---------------------------------------------------------------------------
// Reset mocks between tests
// ---------------------------------------------------------------------------
afterEach(() => {
  vi.restoreAllMocks();
});
