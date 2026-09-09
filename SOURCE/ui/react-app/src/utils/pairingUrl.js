/**
 * Masking for the pairing link shown on the Add Room screen (#4434).
 *
 * The Add Room screen publishes the join URL twice: inside the QR image and as
 * text beside it. The text copy is the one a camera or a screen-share reads
 * most easily, so whatever secret the URL carries is effectively public the
 * moment the screen is captured. The URL now carries only a short-lived,
 * single-use pairing ticket, and this hides even that by default — the user
 * sees where the link points without the code being legible on camera.
 *
 * Only the value is hidden; the host, port and room stay readable so the user
 * can still sanity-check the address they are handing to another device.
 */

// Query parameters whose VALUE must never be shown in plain text on screen.
export const SENSITIVE_PAIRING_PARAMS = ['pair', 'spoke_token'];

const MASK = '••••••••';

/**
 * Return `url` with any pairing secret replaced by dots.
 * Falls back to a string-level substitution for non-absolute/odd URLs so a
 * parse failure can never leak the raw value.
 */
export function maskPairingUrl(url) {
  if (!url) return '';
  const raw = String(url);

  try {
    const parsed = new URL(raw);
    let masked = false;
    SENSITIVE_PAIRING_PARAMS.forEach((param) => {
      if (parsed.searchParams.has(param) && parsed.searchParams.get(param)) {
        parsed.searchParams.set(param, MASK);
        masked = true;
      }
    });
    if (!masked) return raw;
    // URL serialization percent-encodes the mask; put the dots back so the
    // display reads as a mask rather than as another opaque blob.
    return parsed.toString().replace(/%E2%80%A2/g, '•');
  } catch {
    return SENSITIVE_PAIRING_PARAMS.reduce(
      (acc, param) => acc.replace(new RegExp(`([?&]${param}=)[^&]+`, 'g'), `$1${MASK}`),
      raw,
    );
  }
}

/** True when `url` carries a pairing secret that should stay hidden. */
export function hasMaskablePairingSecret(url) {
  return maskPairingUrl(url) !== String(url || '');
}

export default maskPairingUrl;
