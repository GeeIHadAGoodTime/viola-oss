import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AuthProvider, __setInMemorySessionForTest } from '../../auth/AuthProvider';
import { gotrueClient } from '../../lib/gotrue_client';
import CloudAuthGate from './CloudAuthGate';
import SignUpScreen from './SignUpScreen';

const reply = (body, status = 200) => new Response(status === 204 ? null : JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});
let requests;
let unexpected;
beforeEach(async () => {
  window.history.replaceState({}, '', '/');
  delete window.viola;
  localStorage.clear();
  __setInMemorySessionForTest(null);
  requests = [];
  unexpected = [];
  vi.stubGlobal('fetch', vi.fn((input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    if (url.pathname.endsWith('/auth/v1/signup')) {
      const body = JSON.parse(init?.body || '{}');
      let resolve;
      const promise = new Promise(accept => { resolve = accept; });
      requests.push({email: body.email, resolve, promise});
      return promise;
    }
    if (url.pathname.endsWith('/auth/v1/logout')) return Promise.resolve(reply(null, 204));
    unexpected.push(url.pathname);
    return Promise.reject(new Error('Unexpected synthetic request'));
  }));
  await gotrueClient.stopAutoRefresh();
  await gotrueClient.signOut({scope: 'local'});
});
afterEach(async () => {
  cleanup();
  await act(async () => { for (const request of requests) request.resolve(reply({message:'Synthetic refused request'},503)); });
  await gotrueClient.stopAutoRefresh();
  expect(unexpected).toEqual([]);
  vi.restoreAllMocks();vi.unstubAllGlobals();
});
async function start(strict = false) {
  const app = <AuthProvider><CloudAuthGate><div>Signed-in dashboard fixture</div></CloudAuthGate></AuthProvider>;
  const view = render(strict ? <React.StrictMode>{app}</React.StrictMode> : app);
  await screen.findByRole('heading', {name: 'Welcome back'});
  fireEvent.click(screen.getByRole('button', {name:'Create an account'}));
  return view;
}
function fill(email) {
  fireEvent.change(screen.getByLabelText(/^Email$/i), {target: {value:email}});
  fireEvent.change(screen.getByLabelText(/^Password$/i), {target: {value:'synthetic-example-password'}});
  fireEvent.change(screen.getByLabelText(/^Confirm password$/i), {target: {value:'synthetic-example-password'}});
  fireEvent.click(screen.getByLabelText(/age and guardian-consent requirements/i));
  fireEvent.click(screen.getByLabelText(/I agree to Viola's/i));
}
async function submit(email = 'first@example.invalid') {
  fill(email);
  const count = requests.length;
  fireEvent.click(screen.getByRole('button', {name:'Create account'}));
  await waitFor(() => expect(requests).toHaveLength(count+1));
  expect(requests[count].email).toBe(email);
}
async function settle(index, status=200) {
  await act(async () => requests[index].resolve(reply(status===200?{id:'synthetic-unverified-account'}:{message:'Synthetic service unavailable'},status)));
}

describe('Sign-up completion owns the view that submitted it, through the actual provider and client', () => {
  it('preserves current enumeration-safe verification success', async () => {
    await start();await submit();await settle(0);
    expect(screen.getByRole('heading',{name:'Verify your email'})).toBeInTheDocument();
    expect(screen.getByText(/We sent a verification link to first@example.invalid/)).toBeInTheDocument();
  });
  it('preserves current genuine server errors and allows retry', async () => {
    await start();await submit();await settle(0,503);
    expect(screen.getByRole('heading',{name:'Create your account'})).toBeInTheDocument();
    expect(screen.getByText('The account service is temporarily unavailable. Try again shortly.')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Create account'})).toBeEnabled();
  });
  it('does not replace a newer sign-in form or its draft after leaving sign-up', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    fireEvent.change(screen.getByLabelText(/^Email$/i),{target:{value:'login-draft@example.invalid'}});
    await settle(0);
    expect(screen.getByRole('heading',{name:'Welcome back'})).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email$/i)).toHaveValue('login-draft@example.invalid');
  });
  it('does not replace a newer reset form or its draft after leaving sign-up', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    fireEvent.click(screen.getByRole('button',{name:/Forgot password/}));
    fireEvent.change(screen.getByLabelText(/^Email$/i),{target:{value:'reset-draft@example.invalid'}});
    await settle(0);
    expect(screen.getByRole('heading',{name:'Reset your password'})).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email$/i)).toHaveValue('reset-draft@example.invalid');
  });
  it('does not replace a reopened sign-up while its newer request is pending', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    fireEvent.click(screen.getByRole('button',{name:'Create an account'}));
    await submit('second@example.invalid');await settle(0);
    expect(screen.getByRole('heading',{name:'Create your account'})).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email$/i)).toHaveValue('second@example.invalid');
    expect(screen.getByRole('button',{name:'Creating account…'})).toBeDisabled();
    await settle(1);
    expect(screen.getByText(/We sent a verification link to second@example.invalid/)).toBeInTheDocument();
  });
  it('does not replace a newer confirmed verification address with the earlier address', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    fireEvent.click(screen.getByRole('button',{name:'Create an account'}));
    await submit('second@example.invalid');await settle(1);await settle(0);
    expect(screen.getByText(/We sent a verification link to second@example.invalid/)).toBeInTheDocument();
    expect(screen.queryByText(/We sent a verification link to first@example.invalid/)).not.toBeInTheDocument();
  });
  it('keeps a later sign-in screen after an abandoned request is refused', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));await settle(0,503);
    expect(screen.getByRole('heading',{name:'Welcome back'})).toBeInTheDocument();
    expect(screen.queryByText('The account service is temporarily unavailable. Try again shortly.')).not.toBeInTheDocument();
  });
  it('preserves the newer signup server error when the earlier signup succeeds late', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    fireEvent.click(screen.getByRole('button',{name:'Create an account'}));
    await submit('second@example.invalid');await settle(1,503);await settle(0);
    expect(screen.getByRole('heading',{name:'Create your account'})).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email$/i)).toHaveValue('second@example.invalid');
    expect(screen.getByText('The account service is temporarily unavailable. Try again shortly.')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Create account'})).toBeEnabled();
  });
  it('preserves an unsubmitted reopened signup draft after the old response', async () => {
    await start();await submit();
    fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    fireEvent.click(screen.getByRole('button',{name:'Create an account'}));
    fill('unsent@example.invalid');await settle(0);
    expect(screen.getByRole('heading',{name:'Create your account'})).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email$/i)).toHaveValue('unsent@example.invalid');
    expect(screen.getByRole('button',{name:'Create account'})).toBeEnabled();
    expect(requests).toHaveLength(1);
  });
  it('allows a successful current retry without losing the submitted address', async () => {
    await start();await submit();await settle(0,503);
    fireEvent.click(screen.getByRole('button',{name:'Create account'}));
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1].email).toBe('first@example.invalid');await settle(1);
    expect(screen.getByText(/We sent a verification link to first@example.invalid/)).toBeInTheDocument();
  });
  it('admits only one submit when two form events arrive in the same render', async () => {
    await start();fill('single@example.invalid');
    const form = screen.getByRole('button',{name:'Create account'}).closest('form');
    await act(async () => { fireEvent.submit(form);fireEvent.submit(form); });
    expect(requests).toHaveLength(1);await settle(0);
    expect(screen.getByText(/We sent a verification link to single@example.invalid/)).toBeInTheDocument();
  });
  it('allows an ordinary current signup under StrictMode effect replay', async () => {
    await start(true);await submit();await settle(0);
    expect(screen.getByText(/We sent a verification link to first@example.invalid/)).toBeInTheDocument();
  });
  it('does not affect a newly mounted gate after the original gate unmounts', async () => {
    const view=await start();await submit();view.unmount();
    await start();fill('fresh-mount@example.invalid');await settle(0);
    expect(screen.getByRole('heading',{name:'Create your account'})).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email$/i)).toHaveValue('fresh-mount@example.invalid');
    expect(screen.getByRole('button',{name:'Create account'})).toBeEnabled();
  });

  it('retires the completion callback immediately when leaving before parent propagation', async () => {
    const leave=vi.fn();const verify=vi.fn();
    render(<AuthProvider><SignUpScreen onSwitchToLogin={leave} onNeedsVerification={verify} /></AuthProvider>);
    await submit();fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
    expect(leave).toHaveBeenCalledTimes(1);await settle(0);
    expect(verify).not.toHaveBeenCalled();
  });
  it('retires the completion callback on an external form unmount', async () => {
    const verify=vi.fn();const view=render(<AuthProvider><SignUpScreen onSwitchToLogin={vi.fn()} onNeedsVerification={verify} /></AuthProvider>);
    await submit();view.rerender(<AuthProvider><div>Another front-door view</div></AuthProvider>);await settle(0);
    expect(verify).not.toHaveBeenCalled();
    expect(screen.getByText('Another front-door view')).toBeInTheDocument();
  });

});
