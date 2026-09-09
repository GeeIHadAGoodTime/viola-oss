/**
 * Behavioral oracle for issue #2317: TOTP MFA sign-in must be REACHABLE.
 *
 * After the GoTrue migration (bce028c3c) the AccountTab AuthForm lost its 'mfa'
 * mode and lib/auth_context.login() became single-step
 * gotrueClient.signInWithPassword — so an account with a verified TOTP factor
 * signed in to an AAL1 session and was NEVER prompted for the second factor.
 * These tests drive the real AuthForm + real lib/auth_context against a
 * controllable fake gotrueClient and prove the whole second-factor path works
 * end to end: password sign-in on an MFA-enrolled account surfaces the TOTP
 * prompt, a valid code calls GoTrue's mfa.challengeAndVerify and completes
 * sign-in, and a non-MFA account is never prompted.
 *
 * The gotrueClient module is mocked here (not fetch-stubbed) so the AAL step-up
 * gate can be exercised deterministically without hand-rolling the GoTrue
 * challenge/verify REST dance. mfaStepUpForSession only base64-decodes the
 * access token's `aal` claim (no signature check), so an unsigned but
 * structurally valid JWT is a faithful stand-in.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
// vitest hoists the vi.mock() calls below above every import, so the
// gotrue_client / auth/useAuth mocks are registered before these modules load.
import { AuthProvider } from '../hooks/useAuth';
import { AccountTab } from './AccountTab';

const gotrue = vi.hoisted(() => {
  const authStateCbs = [];
  return {
    authStateCbs,
    signInWithPassword: vi.fn(),
    mfa: { challengeAndVerify: vi.fn() },
    getSession: vi.fn(async () => ({ data: { session: null }, error: null })),
    signOut: vi.fn(async () => ({ error: null })),
    onAuthStateChange: vi.fn((cb) => {
      authStateCbs.push(cb);
      return { data: { subscription: { unsubscribe: vi.fn() } } };
    }),
  };
});

vi.mock('../lib/gotrue_client', () => ({
  gotrueClient: gotrue,
  externalOAuthAuthorizeUrl: (url) => url,
}));

// Keep the separate cloud front-door auth context (#1067) out of the way:
// always signed out, so AccountTab's render decision is driven purely by the
// hooks/useAuth (lib/auth_context) session under test.
vi.mock('../auth/useAuth', () => ({
  useAuth: () => ({
    status: 'signedOut',
    user: null,
    session: null,
    signOut: vi.fn(async () => ({ ok: true, error: null })),
  }),
}));

function b64url(obj) {
  return btoa(JSON.stringify(obj)).replace(/=+$/, '');
}

// Unsigned JWT carrying the given AAL claim.
function accessTokenAtAal(aal) {
  return `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url({
    aal,
    sub: 'u-mfa',
    exp: Math.floor(Date.now() / 1000) + 3600,
  })}.sig`;
}

function sessionAtAal(aal, { withTotpFactor = true } = {}) {
  return {
    access_token: accessTokenAtAal(aal),
    refresh_token: 'refresh-token',
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: {
      id: 'u-mfa',
      aud: 'authenticated',
      role: 'authenticated',
      email: 'mfa@example.com',
      app_metadata: { provider: 'email' },
      user_metadata: {},
      email_confirmed_at: '2026-01-01T00:00:00.000Z',
      created_at: '2026-01-01T00:00:00.000Z',
      factors: withTotpFactor
        ? [{ id: 'factor-totp-1', factor_type: 'totp', status: 'verified' }]
        : [],
    },
  };
}

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: () => Promise.resolve({}),
  })));
}

describe('AccountTab — TOTP MFA sign-in (#2317)', () => {
  beforeEach(() => {
    stubFetch();
    gotrue.authStateCbs.length = 0;
    gotrue.signInWithPassword.mockReset();
    gotrue.mfa.challengeAndVerify.mockReset();
    gotrue.getSession.mockReset();
    gotrue.signOut.mockReset();
    // Mount hydration: no existing session.
    gotrue.getSession.mockResolvedValue({ data: { session: null }, error: null });
    gotrue.signOut.mockResolvedValue({ error: null });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('prompts for the TOTP code after password sign-in on an MFA-enrolled account, then completes sign-in', async () => {
    // Password step returns an AAL1 session with a verified TOTP factor.
    gotrue.signInWithPassword.mockResolvedValue({
      data: { session: sessionAtAal('aal1') },
      error: null,
    });
    // The GoTrue AAL2 step-up succeeds and upgrades the stored session.
    gotrue.mfa.challengeAndVerify.mockResolvedValue({
      data: { access_token: accessTokenAtAal('aal2'), user: sessionAtAal('aal2').user },
      error: null,
    });

    const { user } = render(
      <AuthProvider>
        <AccountTab />
      </AuthProvider>,
    );

    await screen.findByRole('button', { name: /^Sign In$/i });
    await user.type(screen.getByLabelText(/^Email$/i), 'mfa@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'CorrectHorse123!');
    await user.click(screen.getByRole('button', { name: /^Sign In$/i }));

    // Second-factor prompt must appear — the pre-fix shape signed straight in.
    await screen.findByRole('heading', { name: /Two-Factor Authentication/i });
    const codeInput = screen.getByLabelText(/Authenticator code/i);
    expect(codeInput).toBeInTheDocument();
    // AAL1 session must NOT flash the signed-in profile.
    expect(screen.queryByRole('button', { name: /^Sign Out$/i })).not.toBeInTheDocument();

    // After signInWithPassword, getSession returns the (now AAL2) session.
    gotrue.getSession.mockResolvedValue({ data: { session: sessionAtAal('aal2') }, error: null });

    await user.type(codeInput, '123456');
    await user.click(screen.getByRole('button', { name: /^Verify$/i }));

    await waitFor(() => expect(gotrue.mfa.challengeAndVerify).toHaveBeenCalledTimes(1));
    expect(gotrue.mfa.challengeAndVerify).toHaveBeenCalledWith({
      factorId: 'factor-totp-1',
      code: '123456',
    });

    // Step-up complete → signed-in profile replaces the TOTP prompt.
    await screen.findByText('mfa@example.com');
    expect(screen.getByRole('button', { name: /^Sign Out$/i })).toBeInTheDocument();
  });

  it('does NOT prompt for a second factor when the account has no verified TOTP factor', async () => {
    gotrue.signInWithPassword.mockResolvedValue({
      data: { session: sessionAtAal('aal1', { withTotpFactor: false }) },
      error: null,
    });

    const { user } = render(
      <AuthProvider>
        <AccountTab />
      </AuthProvider>,
    );

    await screen.findByRole('button', { name: /^Sign In$/i });
    await user.type(screen.getByLabelText(/^Email$/i), 'mfa@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'CorrectHorse123!');
    await user.click(screen.getByRole('button', { name: /^Sign In$/i }));

    // Straight to the signed-in profile; no TOTP detour.
    await screen.findByText('mfa@example.com');
    expect(screen.queryByRole('heading', { name: /Two-Factor Authentication/i })).not.toBeInTheDocument();
    expect(gotrue.mfa.challengeAndVerify).not.toHaveBeenCalled();
  });

  it('cancel from the TOTP prompt signs out the half-authenticated AAL1 session and returns to sign in', async () => {
    gotrue.signInWithPassword.mockResolvedValue({
      data: { session: sessionAtAal('aal1') },
      error: null,
    });

    const { user } = render(
      <AuthProvider>
        <AccountTab />
      </AuthProvider>,
    );

    await screen.findByRole('button', { name: /^Sign In$/i });
    await user.type(screen.getByLabelText(/^Email$/i), 'mfa@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'CorrectHorse123!');
    await user.click(screen.getByRole('button', { name: /^Sign In$/i }));

    await screen.findByRole('heading', { name: /Two-Factor Authentication/i });
    await user.click(screen.getByRole('button', { name: /Cancel and sign in again/i }));

    await waitFor(() => expect(gotrue.signOut).toHaveBeenCalledTimes(1));
    await screen.findByRole('button', { name: /^Sign In$/i });
    expect(screen.queryByRole('heading', { name: /Two-Factor Authentication/i })).not.toBeInTheDocument();
  });

  it('shows an error and stays on the TOTP prompt when the code is rejected', async () => {
    gotrue.signInWithPassword.mockResolvedValue({
      data: { session: sessionAtAal('aal1') },
      error: null,
    });
    gotrue.mfa.challengeAndVerify.mockResolvedValue({
      data: null,
      error: { name: 'AuthError', message: 'Invalid TOTP code entered', code: 'invalid_totp', status: 400 },
    });

    const { user } = render(
      <AuthProvider>
        <AccountTab />
      </AuthProvider>,
    );

    await screen.findByRole('button', { name: /^Sign In$/i });
    await user.type(screen.getByLabelText(/^Email$/i), 'mfa@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'CorrectHorse123!');
    await user.click(screen.getByRole('button', { name: /^Sign In$/i }));

    await screen.findByRole('heading', { name: /Two-Factor Authentication/i });
    await user.type(screen.getByLabelText(/Authenticator code/i), '000000');
    await user.click(screen.getByRole('button', { name: /^Verify$/i }));

    await screen.findByText(/Invalid TOTP code entered/i);
    // Still on the TOTP prompt, not signed in.
    expect(screen.getByRole('heading', { name: /Two-Factor Authentication/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Sign Out$/i })).not.toBeInTheDocument();
  });
});
