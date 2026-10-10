import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '../test/test-utils';
import { AccountTab } from './AccountTab';
import { resetEnabledAuthProvidersCache } from '../auth/authProviders';
import { THEME as theme } from '../config';

const stores = vi.hoisted(() => ({ auth: null, cloud: null }));
vi.mock('../hooks/useAuth', () => ({ useAuth: () => stores.auth }));
vi.mock('../auth/useAuth', () => ({ useAuth: () => stores.cloud }));

const instruction = 'Finish signing in with your browser, then come back here.';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function settle(request, result) {
  await act(async () => { request.resolve(result); });
}

beforeEach(() => {
  resetEnabledAuthProvidersCache();
  window.viola = { openExternalUrl: vi.fn() };
  stores.auth = {
    user: null,
    subscription: null,
    loading: false,
    isLoggedIn: false,
    passwordRecovery: false,
    error: null,
    clearError: vi.fn(),
    startOAuthFlow: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
  };
  stores.cloud = { status: 'signedOut', user: null, signOut: vi.fn() };
  vi.stubGlobal('fetch', vi.fn(async (url) => ({
    ok: true,
    status: 200,
    json: async () => String(url).includes('/auth/v1/settings')
      ? { external: { google: true, apple: true } } : {},
  })));
});

afterEach(() => {
  delete window.viola;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetEnabledAuthProvidersCache();
});

describe('AccountTab desktop OAuth pending instruction', () => {
  it.each(['Google', 'Apple'])('shows browser instructions while %s sign-in is pending', async (provider) => {
    const request = deferred();
    stores.auth.startOAuthFlow.mockReturnValue(request.promise);
    const { user } = render(<AccountTab />);
    await screen.findByRole('button', { name: `Continue with ${provider}` });
    const email = screen.getByLabelText('Email');
    await user.type(email, 'synthetic@example.invalid');

    await user.click(screen.getByRole('button', { name: `Continue with ${provider}` }));

    expect(stores.auth.startOAuthFlow).toHaveBeenCalledExactlyOnceWith(provider.toLowerCase());
    expect(screen.getByText(instruction)).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent(instruction);
    expect(screen.getByRole('status')).toHaveStyle({ color: theme.colors.accent });
    expect(email).toHaveValue('synthetic@example.invalid');
    expect(screen.getByRole('heading', { name: 'Sign In' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Sign Out' })).not.toBeInTheDocument();
    await settle(request, { success: true });
    expect(email).toHaveValue('synthetic@example.invalid');
  });

  it('clears the instruction and releases the controls on success without inventing a session', async () => {
    const request = deferred();
    stores.auth.startOAuthFlow.mockReturnValue(request.promise);
    const { user } = render(<AccountTab />);
    const google = await screen.findByRole('button', { name: 'Continue with Google' });
    await user.click(google);

    await settle(request, { success: true });

    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
    expect(google).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Continue with Apple' })).toBeEnabled();
    // Only the auth store owns the session; a UI message cannot authenticate.
    expect(screen.getByRole('heading', { name: 'Sign In' })).toBeVisible();
    expect(stores.auth.login).not.toHaveBeenCalled();
  });

  it('replaces the instruction with the failure and starts a clean explicit retry', async () => {
    const first = deferred();
    const retry = deferred();
    stores.auth.startOAuthFlow.mockReturnValueOnce(first.promise).mockReturnValueOnce(retry.promise);
    const { user } = render(<AccountTab />);
    const google = await screen.findByRole('button', { name: 'Continue with Google' });
    const email = screen.getByLabelText('Email');
    await user.type(email, 'synthetic@example.invalid');
    await user.click(google);
    await settle(first, { success: false, error: 'Sign-in was cancelled. Please try again.' });

    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
    expect(screen.getByText('Sign-in was cancelled. Please try again.')).toBeVisible();
    expect(email).toHaveValue('synthetic@example.invalid');
    expect(google).toBeEnabled();
    await user.click(google);

    expect(screen.queryByText('Sign-in was cancelled. Please try again.')).not.toBeInTheDocument();
    expect(screen.getByText(instruction)).toBeVisible();
    expect(stores.auth.startOAuthFlow).toHaveBeenCalledTimes(2);
    expect(stores.auth.clearError).toHaveBeenCalledTimes(2);
    await settle(retry, { success: true });
    expect(email).toHaveValue('synthetic@example.invalid');
    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
    expect(google).toBeEnabled();
  });

  it('keeps both providers and email submission disabled during repeated clicks', async () => {
    const request = deferred();
    stores.auth.startOAuthFlow.mockReturnValue(request.promise);
    const { user } = render(<AccountTab />);
    const google = await screen.findByRole('button', { name: 'Continue with Google' });
    const apple = screen.getByRole('button', { name: 'Continue with Apple' });
    await user.type(screen.getByLabelText('Email'), 'synthetic@example.invalid');
    await user.type(screen.getByLabelText('Password'), 'synthetic-password');
    await user.click(google);

    expect(google).toBeDisabled();
    expect(apple).toBeDisabled();
    const submit = screen.getByRole('button', { name: 'Please wait...' });
    expect(submit).toBeDisabled();
    await user.click(google);
    await user.click(apple);
    await user.click(submit);
    expect(stores.auth.startOAuthFlow).toHaveBeenCalledExactlyOnceWith('google');
    expect(stores.auth.login).not.toHaveBeenCalled();
    await settle(request, { success: false, error: 'Sign-in timed out.' });
    expect(screen.getByRole('button', { name: 'Sign In', exact: true })).toBeEnabled();
    expect(screen.getByText('Sign-in timed out.')).toBeVisible();
    expect(screen.getByLabelText('Email')).toHaveValue('synthetic@example.invalid');
    expect(screen.getByLabelText('Password')).toHaveValue('synthetic-password');
  });

  it('clears the instruction when changing form modes and does not restore it on completion', async () => {
    const request = deferred();
    stores.auth.startOAuthFlow.mockReturnValue(request.promise);
    const { user } = render(<AccountTab />);
    await user.click(await screen.findByRole('button', { name: 'Continue with Google' }));
    expect(screen.getByText(instruction)).toBeVisible();

    await user.click(screen.getByRole('button', { name: 'Sign up' }));
    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
    await settle(request, { success: true });
    expect(screen.getByRole('heading', { name: 'Create Account' })).toBeVisible();
    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create Account' })).toBeDisabled();
  });

  it.each([true, false])('does not let a dismissed form completion (success=%s) clear a reopened pending instruction', async (success) => {
    const oldRequest = deferred();
    const currentRequest = deferred();
    stores.auth.startOAuthFlow.mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(currentRequest.promise);
    const first = render(<AccountTab />);
    await first.user.click(await screen.findByRole('button', { name: 'Continue with Google' }));
    first.unmount();

    const reopened = render(<AccountTab />);
    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
    await reopened.user.click(await screen.findByRole('button', { name: 'Continue with Apple' }));
    await settle(oldRequest, { success, error: success ? undefined : 'Old cancelled sign-in.' });

    expect(screen.getByText(instruction)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Continue with Apple' })).toBeDisabled();
    expect(screen.queryByText('Old cancelled sign-in.')).not.toBeInTheDocument();
    await settle(currentRequest, { success: true });
    expect(screen.queryByText(instruction)).not.toBeInTheDocument();
  });
});
