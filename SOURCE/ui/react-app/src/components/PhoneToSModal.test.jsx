import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '../test/test-utils';
import { clearCachedClientApiKey, setCloudSession } from '../config';
import PhoneToSModal, { PHONE_TOS_AUDIT_NOTICE } from './PhoneToSModal';

// Keep PhoneToSModal, authFetch, and the CSRF helper real. Only the network
// boundary is stubbed so these checks catch a missing header in the actual path.
function renderOpenModal() {
  const onClose = vi.fn();
  const onAccepted = vi.fn();
  function Harness() {
    const [isOpen, setIsOpen] = useState(true);
    return (
      <PhoneToSModal
        isOpen={isOpen}
        onClose={() => {
          onClose();
          setIsOpen(false);
        }}
        onAccepted={onAccepted}
        payload={{ error_code: 'phone_tos_required' }}
      />
    );
  }
  return { ...render(<Harness />), onClose, onAccepted };
}

function acceptedResponse() {
  return {
    ok: true,
    status: 200,
    json: async () => ({ ok: true, data: { accepted: true } }),
  };
}

describe('PhoneToSModal', () => {
  beforeEach(() => {
    vi.stubGlobal('__VIOLA_API_KEY__', 'desktop-api-key');
    clearCachedClientApiKey();
    // A desktop that also has a cloud session must still use local authority.
    setCloudSession({ access_token: 'raw-cloud-jwt' });
    document.cookie = 'viola_csrf=desktop-csrf-token; Path=/';
  });

  afterEach(() => {
    document.cookie = 'viola_csrf=; Max-Age=0; Path=/';
    setCloudSession(null);
    clearCachedClientApiKey();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('posts through real desktop authFetch with CSRF and closes on success', async () => {
    const fetchSpy = vi.fn(async () => acceptedResponse());
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /i agree/i }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(fetchSpy).toHaveBeenCalledExactlyOnceWith('/v1/phone/accept-tos', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      headers: expect.objectContaining({
        'X-API-Key': 'desktop-api-key',
        'X-CSRF-Token': 'desktop-csrf-token',
      }),
    }));
    const [, { headers }] = fetchSpy.mock.calls[0];
    expect(Object.keys(headers).some((name) => name.toLowerCase() === 'authorization')).toBe(false);
    expect(onAccepted).toHaveBeenCalledWith({ accepted: true });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('keeps the dialog open on a backend rejection and retries with a rotated cookie', async () => {
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        json: async () => ({ ok: false, error: { message: 'CSRF token mismatch' } }),
      })
      .mockResolvedValueOnce(acceptedResponse());
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();

    await user.click(screen.getByRole('button', { name: /i agree/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('CSRF token mismatch');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /i agree/i })).toBeEnabled();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    document.cookie = 'viola_csrf=rotated-csrf-token; Path=/';
    await user.click(screen.getByRole('button', { name: /i agree/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(fetchSpy.mock.calls.map(([, options]) => options.headers['X-CSRF-Token']))
      .toEqual(['desktop-csrf-token', 'rotated-csrf-token']);
    expect(onAccepted).toHaveBeenCalledExactlyOnceWith({ accepted: true });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not pretend acceptance succeeded when no readable CSRF cookie exists', async () => {
    document.cookie = 'viola_csrf=; Max-Age=0; Path=/';
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ ok: false, error: { message: 'CSRF token missing' } }),
    });
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();

    await user.click(screen.getByRole('button', { name: /i agree/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('CSRF token missing');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0][1].headers['X-CSRF-Token']).toBeUndefined();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not submit twice or cancel while acceptance is pending', async () => {
    let resolveAcceptance;
    const response = new Promise((resolve) => { resolveAcceptance = resolve; });
    const fetchSpy = vi.fn().mockReturnValue(response);
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();
    const agree = screen.getByRole('button', { name: /i agree/i });

    await user.dblClick(agree);

    expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled();
    const cancel = screen.getByRole('button', { name: /cancel/i });
    expect(cancel).toBeDisabled();
    await user.click(agree);
    await user.click(cancel);
    await user.click(screen.getByRole('button', { name: /close modal/i }));
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('dialog').parentElement);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => resolveAcceptance(acceptedResponse()));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(onAccepted).toHaveBeenCalledExactlyOnceWith({ accepted: true });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Cancel without posting or recording acceptance', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();

    await user.click(screen.getByRole('button', { name: /cancel/i }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  async function dismiss(user, method) {
    if (method === 'Escape') await user.keyboard('{Escape}');
    else if (method === 'backdrop') await user.click(screen.getByRole('dialog').parentElement);
    else await user.click(screen.getByRole('button', { name: /close modal/i }));
  }

  it.each(['X', 'Escape', 'backdrop'])('allows %s dismissal before submitting without acceptance', async (method) => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();

    await dismiss(user, method);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it.each(['X', 'Escape', 'backdrop'])('blocks %s while submitting and permits it after failure', async (method) => {
    let resolveAcceptance;
    const pending = new Promise((resolve) => { resolveAcceptance = resolve; });
    const fetchSpy = vi.fn().mockReturnValue(pending);
    vi.stubGlobal('fetch', fetchSpy);
    const { user, onClose, onAccepted } = renderOpenModal();
    await user.click(screen.getByRole('button', { name: /i agree/i }));

    await dismiss(user, method);

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    await act(async () => resolveAcceptance({
      ok: false,
      status: 503,
      json: async () => ({ ok: false, error: { message: 'Cloud unavailable' } }),
    }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Cloud unavailable');
    await dismiss(user, method);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onAccepted).not.toHaveBeenCalled();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('shows the phone auditability default notice before acceptance', () => {
    const onClose = vi.fn();

    render(
      <PhoneToSModal
        isOpen={true}
        onClose={onClose}
        payload={{ error_code: 'phone_tos_required' }}
      />
    );

    expect(screen.getByText('Auditability by default')).toBeInTheDocument();
    expect(screen.getByText(PHONE_TOS_AUDIT_NOTICE)).toBeInTheDocument();
    expect(PHONE_TOS_AUDIT_NOTICE).toContain('recorded and transcribed by default for your audit trail');
    expect(PHONE_TOS_AUDIT_NOTICE).toContain('proactive AI announcement');
    expect(PHONE_TOS_AUDIT_NOTICE).toContain('Phone Settings');
  });
});
