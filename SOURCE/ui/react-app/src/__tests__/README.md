# Viola React Test Suite

## Running Tests

```bash
cd ui/react-app

# Interactive watch mode (re-runs on file changes)
npm test

# Single run (CI mode)
npm run test:run

# With coverage report
npm run test:coverage
```

## Test Structure

```
src/
├── test/
│   ├── setup.js              # Global test setup (mocks for WebSocket, fetch, etc.)
│   └── test-utils.jsx        # Custom render with providers + userEvent
├── lib/
│   └── eventBus.test.js      # Unit tests for EventBus class
└── __tests__/
    ├── SmartDisplay.test.jsx  # Smoke test — mounts SmartDisplay with mocked hooks
    ├── hooks/
    │   ├── useEventBus.test.jsx       # useEventBus and useEventEmitter hooks
    │   └── usePlaybackPosition.test.js # Playback position interpolation
    ├── utils/
    │   └── formatters.test.js         # timeFormat, artProxy utilities
    └── integration/
        ├── ErrorBoundary.test.jsx     # Error catching and recovery
        └── Toast.test.jsx            # Toast system: useToast hook + ToastContainer
```

## Writing New Tests

### Import the custom render

Always import from `test-utils` instead of `@testing-library/react` directly:

```jsx
import { render, screen } from '../test/test-utils';
```

This gives you all RTL exports plus a `user` instance for simulating interactions:

```jsx
const { user } = render(<MyComponent />);
await user.click(screen.getByRole('button'));
```

### Mocking Patterns

#### WebSocket
WebSocket is globally mocked in `setup.js`. For hook-level tests, mock `useWebSocket`:

```jsx
vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: (onMessage) => ({
    send: vi.fn(),
    connectCount: 0,
    setBinaryCallback: vi.fn(),
    setDisconnectCallback: vi.fn(),
    getWsDebug: vi.fn(() => ({})),
  }),
}));
```

#### Fetch / API calls
`fetch` is globally mocked. Override per-test:

```jsx
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ data: { volume: 80 } }),
  })
);
```

Or mock `useViolaApi` at the hook level for component tests.

#### Audio API
`HTMLMediaElement` methods are not implemented in jsdom. Mock them:

```jsx
window.HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve());
window.HTMLMediaElement.prototype.pause = vi.fn();
```

### Testing Hooks

Use `renderHook` from `@testing-library/react`:

```jsx
import { renderHook, act } from '@testing-library/react';

const { result } = renderHook(() => useMyHook());
act(() => { result.current.doSomething(); });
expect(result.current.value).toBe(42);
```

### Fake Timers

For time-dependent tests (position interpolation, auto-dismiss):

```jsx
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

act(() => { vi.advanceTimersByTime(1000); });
```
