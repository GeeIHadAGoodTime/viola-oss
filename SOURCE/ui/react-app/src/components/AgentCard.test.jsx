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

// 1.0.5 build 5: when a reminder fired, the card showed the stored action
// (`viola-tool-action:v1:{"args":...`) and "Waiting for reasoning...".
describe('AgentCard for scheduled work', () => {
  const encoded = 'viola-tool-action:v1:{"args":{"channel":null,"message":"Stretch"},"tool":"notify"}';

  it('never shows an encoded scheduler action', () => {
    render(<AgentCard agent={{ ...baseAgent, task: encoded, reason: 'scheduler:9', status: 'completed' }} onCancel={vi.fn()} />);
    expect(screen.getByText('Reminder: Stretch')).toBeInTheDocument();
    expect(screen.queryByText(/viola-tool-action/)).not.toBeInTheDocument();
    expect(screen.queryByText('Waiting for reasoning...')).not.toBeInTheDocument();
  });

  it('labels alarms and sleep timers', async () => {
    const { agentTaskTitle } = await import('./AgentCard');
    expect(agentTaskTitle({ task: 'viola-tool-action:v1:{"args":{"action":"sound","label":"Wake up"},"tool":"alarm"}' })).toBe('Alarm: Wake up');
    expect(agentTaskTitle({ task: 'viola-tool-action:v1:{"args":{"action":"stop","reason":"sleep_timer"},"tool":"playback"}' })).toBe('Sleep timer');
    expect(agentTaskTitle({ task: 'viola-tool-action:v1:not json' })).toBe('Scheduled task');
    expect(agentTaskTitle({ task: 'Book a table' })).toBe('Book a table');
  });

  it('describes a running scheduled task instead of waiting for reasoning', () => {
    render(<AgentCard agent={{ ...baseAgent, task: 'Reminder: Stretch', subagent_type: 'scheduler' }} onCancel={vi.fn()} />);
    expect(screen.getByText('Running a scheduled task')).toBeInTheDocument();
  });
});
