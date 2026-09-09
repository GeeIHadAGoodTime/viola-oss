import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '../test/test-utils';
import LoginPromptModal from './LoginPromptModal';

describe('LoginPromptModal', () => {
  it('opens on login_required_for_paid_action and routes CTA to sign-in', async () => {
    const onClose = vi.fn();
    const onSignIn = vi.fn();

    const { user } = render(
      <LoginPromptModal
        isOpen={true}
        onClose={onClose}
        onSignIn={onSignIn}
        payload={{ error_code: 'login_required_for_paid_action', message: 'Sign in to make phone calls.' }}
      />
    );

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Sign in to make phone calls.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /sign in to continue/i }));

    expect(onSignIn).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
