/* eslint react/jsx-uses-vars: "error" */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import AgentCard from './AgentCard';

const baseAgent = {
  agent_id: 'agent-1',
  task: 'Research reliable local electricians and summarize the best options',
  status: 'running',
  elapsed_seconds: 65,
};

describe('AgentCard', () => {
  it('renders an active agent with empty thinking text', () => {
    render(<AgentCard agent={baseAgent} thinkingText="" onCancel={vi.fn()} />);

    expect(screen.getByText(/Research reliable local electricians/i)).toBeInTheDocument();
    expect(screen.getByText('Waiting for reasoning...')).toBeInTheDocument();
    expect(screen.getByText(/Running 01:05/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Cancel agent/i })).toBeEnabled();
  });

  it('renders the latest thinking text', () => {
    const thinking = 'Checking constraints, comparing sources, and narrowing the final recommendation.';

    render(<AgentCard agent={baseAgent} thinkingText={thinking} onCancel={vi.fn()} />);

    expect(screen.getByText(/comparing sources/i)).toBeInTheDocument();
    expect(screen.queryByText('Waiting for reasoning...')).not.toBeInTheDocument();
  });

  it('renders cancelled state and disables cancellation', () => {
    render(
      <AgentCard
        agent={{ ...baseAgent, status: 'cancelled' }}
        thinkingText="Stopping the background work."
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByTestId('agent-card')).toHaveAttribute('data-status', 'cancelled');
    expect(screen.getByText(/Cancelled 01:05/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Cancel agent/i })).toBeDisabled();
  });
});
