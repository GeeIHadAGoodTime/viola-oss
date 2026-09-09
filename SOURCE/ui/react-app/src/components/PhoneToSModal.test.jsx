import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import PhoneToSModal, { PHONE_TOS_AUDIT_NOTICE } from './PhoneToSModal';

describe('PhoneToSModal', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('posts accept-tos and closes on success', async () => {
    const onClose = vi.fn();
    const onAccepted = vi.fn();
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { accepted: true } }),
    }));
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = render(
      <PhoneToSModal
        isOpen={true}
        onClose={onClose}
        onAccepted={onAccepted}
        payload={{ error_code: 'phone_tos_required' }}
      />
    );

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /i agree/i }));

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith('/v1/phone/accept-tos', expect.objectContaining({ method: 'POST' }));
    });
    expect(onAccepted).toHaveBeenCalledWith({ accepted: true });
    expect(onClose).toHaveBeenCalledTimes(1);
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
