import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import CloudWelcome from './CloudWelcome';

describe('CloudWelcome', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <CloudWelcome isOpen={false} onFinish={vi.fn()} onSkip={vi.fn()} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('opens on the first step and walks Next through all three steps', async () => {
    const onFinish = vi.fn();
    const { user } = render(
      <CloudWelcome isOpen={true} onFinish={onFinish} onSkip={vi.fn()} />
    );

    expect(screen.getByRole('dialog', { name: 'Welcome to Viola' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('dialog', { name: "You're using Viola in your browser" })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('dialog', { name: 'Get the desktop app' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /download desktop app/i })).toHaveAttribute(
      'href',
      'https://useviola.com/download',
    );

    // Last step's primary button reads "Got it" and finishes the tour.
    await user.click(screen.getByRole('button', { name: 'Got it' }));
    expect(onFinish).toHaveBeenCalledTimes(1);
  });

  it('calls onSkip from the Skip button on any step', async () => {
    const onSkip = vi.fn();
    const { user } = render(
      <CloudWelcome isOpen={true} onFinish={vi.fn()} onSkip={onSkip} />
    );

    await user.click(screen.getByRole('button', { name: 'Skip' }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });

  it('calling onSkip via the modal close (Escape) also fires onSkip, never onFinish', async () => {
    const onSkip = vi.fn();
    const onFinish = vi.fn();
    const { user } = render(
      <CloudWelcome isOpen={true} onFinish={onFinish} onSkip={onSkip} />
    );

    await user.keyboard('{Escape}');
    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it('shows a saving state on the last step while finishing', async () => {
    const { user, rerender } = render(
      <CloudWelcome isOpen={true} saving={false} onFinish={vi.fn()} onSkip={vi.fn()} />
    );
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('button', { name: 'Got it' })).toBeInTheDocument();

    rerender(<CloudWelcome isOpen={true} saving={true} onFinish={vi.fn()} onSkip={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Finishing...' })).toBeInTheDocument();
  });

  it('resets to the first step each time it reopens', async () => {
    const { user, rerender } = render(
      <CloudWelcome isOpen={true} onFinish={vi.fn()} onSkip={vi.fn()} />
    );
    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('dialog', { name: "You're using Viola in your browser" })).toBeInTheDocument();

    rerender(<CloudWelcome isOpen={false} onFinish={vi.fn()} onSkip={vi.fn()} />);
    rerender(<CloudWelcome isOpen={true} onFinish={vi.fn()} onSkip={vi.fn()} />);
    expect(screen.getByRole('dialog', { name: 'Welcome to Viola' })).toBeInTheDocument();
  });
});
