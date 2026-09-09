import { describe, it, expect } from 'vitest';
import { maskPairingUrl, hasMaskablePairingSecret } from '../../utils/pairingUrl';

describe('maskPairingUrl', () => {
  it('hides the pairing code while keeping the address readable', () => {
    const masked = maskPairingUrl('http://192.168.1.23:8756/?pair=vpair1.abc.123.deadbeef&room=kitchen');

    expect(masked).not.toContain('vpair1.abc.123.deadbeef');
    expect(masked).toContain('192.168.1.23:8756');
    expect(masked).toContain('room=kitchen');
    expect(masked).toContain('•');
  });

  it('also hides a legacy spoke credential, so an old hub payload cannot leak one', () => {
    const masked = maskPairingUrl('http://192.168.1.23:8756/?spoke_token=vspk1.dev.1.sig&room=kitchen');

    expect(masked).not.toContain('vspk1.dev.1.sig');
    expect(hasMaskablePairingSecret('http://192.168.1.23:8756/?spoke_token=vspk1.dev.1.sig&room=kitchen')).toBe(true);
  });

  it('leaves a URL with no secret exactly as it is', () => {
    const plain = 'http://192.168.1.23:8756/?room=kitchen';

    expect(maskPairingUrl(plain)).toBe(plain);
    expect(hasMaskablePairingSecret(plain)).toBe(false);
  });

  it('never returns the raw value when the URL cannot be parsed', () => {
    const masked = maskPairingUrl('not-a-url/?pair=super-secret-code&room=kitchen');

    expect(masked).not.toContain('super-secret-code');
  });

  it('handles empty input without throwing', () => {
    expect(maskPairingUrl('')).toBe('');
    expect(maskPairingUrl(null)).toBe('');
    expect(maskPairingUrl(undefined)).toBe('');
  });
});
