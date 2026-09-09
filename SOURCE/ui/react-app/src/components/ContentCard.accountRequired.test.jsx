/**
 * The account-required card is the wall a brand-new install hits on its first
 * managed command, so where its buttons go decides whether anyone ever gets
 * past it.
 *
 * They used to open https://useviola.com/login. Inside the Qt shell that is not
 * even the system browser — window.open lands in createWindow
 * (ui/qt_native/webview_window.py:1284-1307) and spawns an in-app popup titled
 * "Sign In" showing the marketing site. So the user signs in, a Viola-looking
 * window confirms it, and they are still blocked: a desktop session is minted
 * only by the desktop's own GoTrue relay, and nothing accepts a session created
 * anywhere else.
 *
 * The payload below is the literal output of core/account_gate
 * .account_required_card(), so if the two halves of this door drift apart these
 * tests notice.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '../test/test-utils';
import ContentCard from './ContentCard';

const ACCOUNT_REQUIRED_CARD = {
  type: 'account_required',
  title: 'Account required',
  body: 'Sign in or create a Viola account to use managed AI.',
  cta: { label: 'Sign in', action: 'open_account_settings', url: 'https://useviola.com/login' },
  secondary_cta: {
    label: 'Create account',
    action: 'open_account_settings',
    url: 'https://useviola.com/login?tab=register',
  },
  dismiss_after_ms: 90000,
};

describe('the account-required block', () => {
  let uiActions;
  let listener;

  beforeEach(() => {
    vi.restoreAllMocks();
    uiActions = [];
    listener = (event) => uiActions.push(event.detail);
    window.addEventListener('viola:ui-action', listener);
  });

  afterEach(() => {
    window.removeEventListener('viola:ui-action', listener);
    vi.restoreAllMocks();
  });

  it('sends "Sign in" to the in-app account panel, not out of the app', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { user } = render(<ContentCard data={ACCOUNT_REQUIRED_CARD} />);

    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(uiActions).toEqual([{ action: 'open_settings', payload: { tab: 'account' } }]);
    expect(openSpy).not.toHaveBeenCalled();
  });

  it('sends "Create account" to the same place', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { user } = render(<ContentCard data={ACCOUNT_REQUIRED_CARD} />);

    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(uiActions).toEqual([{ action: 'open_settings', payload: { tab: 'account' } }]);
    expect(openSpy).not.toHaveBeenCalled();
  });

  it('never opens useviola.com for a CTA that carries the in-app action', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { user } = render(<ContentCard data={ACCOUNT_REQUIRED_CARD} />);

    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    const openedUrls = openSpy.mock.calls.map((call) => call[0]);
    expect(openedUrls).not.toContain('https://useviola.com/login');
    expect(openedUrls).toHaveLength(0);
  });

  it('still opens a URL for a surface whose CTA has no in-app action', async () => {
    // Messaging channels render `url` CTAs as links and have no sign-in form of
    // their own, so the URL fallback has to keep working for them.
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { user } = render(
      <ContentCard
        data={{
          ...ACCOUNT_REQUIRED_CARD,
          cta: { label: 'Sign in', url: 'https://useviola.com/login' },
          secondary_cta: undefined,
        }}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(openSpy).toHaveBeenCalled();
    expect(uiActions).toEqual([]);
  });
});
