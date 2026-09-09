/**
 * Custom render function that wraps components with any required providers.
 *
 * Usage:
 *   import { render, screen } from '../test/test-utils';
 *   render(<MyComponent />);
 *   expect(screen.getByText('hello')).toBeInTheDocument();
 */
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { UiStateProvider } from '../state/uiState';

/**
 * Provider wrapper for all tests. Add context providers here as needed.
 * Currently empty — Viola components are mostly self-contained.
 */
function AllTheProviders({ children }) {
  return <UiStateProvider>{children}</UiStateProvider>;
}

/**
 * Custom render that wraps UI in providers and sets up userEvent.
 * Returns everything from RTL's render plus a `user` instance.
 */
function customRender(ui, options = {}) {
  const user = userEvent.setup();
  const result = render(ui, { wrapper: AllTheProviders, ...options });
  return { ...result, user };
}

// Re-export everything from RTL
export * from '@testing-library/react';

// Override render with our custom version
export { customRender as render };
