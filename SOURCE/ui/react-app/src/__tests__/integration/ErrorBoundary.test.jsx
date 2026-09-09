/**
 * Tests for ErrorBoundary component.
 *
 * Verifies error catching, fallback UI rendering, and retry behavior.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '../../test/test-utils';
import React from 'react';
import ErrorBoundary from '../../components/ErrorBoundary';

// A component that throws on render
const ThrowingChild = ({ shouldThrow = true }) => {
  if (shouldThrow) throw new Error('Test crash');
  return <div>Normal content</div>;
};

describe('ErrorBoundary', () => {
  beforeEach(() => {
    // Suppress React's console.error for intentional throws
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('should render children when there is no error', () => {
    render(
      <ErrorBoundary name="Test">
        <div>Hello World</div>
      </ErrorBoundary>
    );

    expect(screen.getByText('Hello World')).toBeInTheDocument();
  });

  it('should catch errors and show fallback UI', () => {
    render(
      <ErrorBoundary name="Player">
        <ThrowingChild />
      </ErrorBoundary>
    );

    expect(screen.getByText("Player couldn't load")).toBeInTheDocument();
  });

  it('should show a retry button in error state', () => {
    render(
      <ErrorBoundary name="Widget">
        <ThrowingChild />
      </ErrorBoundary>
    );

    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('should use default name "Widget" when no name prop', () => {
    render(
      <ErrorBoundary>
        <ThrowingChild />
      </ErrorBoundary>
    );

    expect(screen.getByText("Widget couldn't load")).toBeInTheDocument();
  });

  it('should recover when retry is clicked and child no longer throws', async () => {
    // Use a mutable ref to control throwing behavior without needing new props.
    // When ErrorBoundary resets after retry, it re-renders the same children tree.
    let shouldThrow = true;
    const ConditionalThrower = () => {
      if (shouldThrow) throw new Error('Test crash');
      return <div>Normal content</div>;
    };

    const { user } = render(
      <ErrorBoundary name="Test">
        <ConditionalThrower />
      </ErrorBoundary>
    );

    expect(screen.getByText("Test couldn't load")).toBeInTheDocument();

    // "Fix" the child before clicking retry
    shouldThrow = false;
    await user.click(screen.getByRole('button', { name: /try again/i }));

    expect(screen.getByText('Normal content')).toBeInTheDocument();
  });
});
