/**
 * SpokePairingGate — LAN-local join gate for browser/phone spokes.
 *
 * A spoke device reaches the hub by scanning the Add Room QR (which carries a
 * short-lived, single-use ?pair= TICKET — never a credential, #4434) or by
 * typing the pairing word at /connect (which redirects to
 * http://<hub-ip>:8756/?room=<room> with nothing at all). Either way it arrives
 * here without a credential, and App.jsx renders this gate BEFORE SpokeWrapper.
 *
 * Scanned QR: the ticket is exchanged automatically (POST /bootstrap/claim) and
 * the user never sees this screen — pairing stays one scan. An expired or
 * already-used ticket (a photographed QR) falls through to the PIN flow below
 * with an explanation, so the device is never stranded.
 *
 * The pairing word is a reversible encoding of the hub's LAN IP, not a secret —
 * so reachability alone must never earn a spoke credential. This gate makes the
 * device prove it can read a short-lived PIN shown on the desktop hub:
 *
 *   1. On mount, POST /bootstrap/request — the hub mints a short-lived numeric
 *      code, stashes a pairing session, and DISPLAYS the code on its own screen
 *      (the desktop Add Room panel polls /v1/network/pair/pending). The code is
 *      never returned to this device.
 *   2. The user reads the code off the desktop and enters it here.
 *   3. POST /bootstrap/confirm — on a correct code the hub mints a spoke
 *      credential via the existing issuer (issue_spoke_credential) and returns
 *      it (and sets the HttpOnly viola_spoke cookie). We then reload into
 *      /?spoke_token=...&room=... so SpokeWrapper + both sockets carry the
 *      token and audio + playback-position sync work.
 *
 * Wrong / missing / expired / brute-forced codes are refused by the hub (TTL,
 * constant-time compare, attempt cap), so neither socket ever accepts the
 * device without a correct PIN.
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';

// Matches ui/security/bootstrap.py _PAIRING_CODE_LENGTH.
const CODE_LENGTH = 6;

function normalizeCode(raw) {
  return (raw || '').replace(/\D/g, '').slice(0, CODE_LENGTH);
}

async function parseDetail(response) {
  // FastAPI HTTPException bodies are {detail: "..."}; success envelopes are
  // {ok, error, data}. Be defensive about non-JSON bodies.
  try {
    const body = await response.json();
    if (body && typeof body.detail === 'string') return body.detail;
    if (body && body.detail && typeof body.detail.message === 'string') return body.detail.message;
    return null;
  } catch {
    return null;
  }
}

export default function SpokePairingGate({ room, ticket }) {
  const [code, setCode] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [status, setStatus] = useState('requesting'); // requesting | ready | submitting | error
  const [banner, setBanner] = useState('');
  const [bannerKind, setBannerKind] = useState('info'); // info | error
  // Scanned-QR path: hold the PIN UI back while the ticket is exchanged, so a
  // successful scan never flashes a code prompt at the user.
  const [claiming, setClaiming] = useState(Boolean(ticket));
  const inputRef = useRef(null);

  // Land in spoke mode with the credential this device just earned.
  const enterSpokeMode = useCallback(
    (spokeToken) => {
      const params = new URLSearchParams();
      // The confirm/claim response also set the HttpOnly viola_spoke cookie, so
      // the token reaches both sockets even without the query param; we add it
      // so the audio-stream URL (which reads ?spoke_token=) carries it too.
      if (spokeToken) params.set('spoke_token', spokeToken);
      if (room) params.set('room', room);
      window.location.replace(`/?${params.toString()}`);
    },
    [room],
  );

  // Request a fresh pairing session. Triggers the desktop to display the code.
  // `explain` is the message to show once the new code is up: a caller that
  // set a banner first would have it wiped by this request, so the reason the
  // user is being asked for a code travels with the request instead.
  const requestSession = useCallback(async (explain = '') => {
    setStatus('requesting');
    setBanner('');
    setCode('');
    try {
      const res = await fetch('/bootstrap/request', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (!res.ok) {
        const detail = await parseDetail(res);
        setStatus('error');
        setBannerKind('error');
        if (res.status === 403) {
          setBanner(detail || 'Pairing is only available from a device on the same Wi-Fi network.');
        } else if (res.status === 429) {
          setBanner(detail || 'Too many attempts. Wait a moment, then try again.');
        } else {
          setBanner(detail || 'Could not start pairing. Reload and try again.');
        }
        return;
      }
      const body = await res.json();
      const data = (body && body.data) || {};
      if (!data.pairing_session_id) {
        setStatus('error');
        setBannerKind('error');
        setBanner('Could not start pairing. Reload and try again.');
        return;
      }
      setSessionId(data.pairing_session_id);
      setStatus('ready');
      setBannerKind(explain ? 'error' : 'info');
      setBanner(explain);
    } catch {
      setStatus('error');
      setBannerKind('error');
      setBanner('Could not reach Viola on this network. Make sure both devices are on the same Wi-Fi.');
    }
  }, []);

  // Scanned QR: exchange the single-use ticket for a real credential without
  // bothering the user. Only if that fails do we fall back to asking for a
  // code — which is also why no PIN session is requested up front on this
  // path: a successful scan must not flash a code prompt here, nor make the
  // desktop display one for a device that is already joining.
  useEffect(() => {
    if (!ticket) return undefined;
    let cancelled = false;
    (async () => {
      let explain = 'That pairing link is no longer valid. Enter the code shown on your Viola desktop instead.';
      try {
        const res = await fetch('/bootstrap/claim', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticket }),
        });
        if (cancelled) return;
        if (res.ok) {
          const body = await res.json();
          const data = (body && body.data) || {};
          if (data.auth_required === false || data.spoke_token) {
            // Navigating away: leave the joining state on screen.
            enterSpokeMode(data.spoke_token);
            return;
          }
        }
        explain = (await parseDetail(res)) || explain;
      } catch {
        if (cancelled) return;
        explain = 'Could not reach Viola. Check your Wi-Fi and try again.';
      }
      if (cancelled) return;
      setClaiming(false);
      await requestSession(explain);
    })();
    return () => {
      cancelled = true;
    };
  }, [ticket, enterSpokeMode, requestSession]);

  useEffect(() => {
    if (ticket) return;
    requestSession();
  }, [ticket, requestSession]);

  useEffect(() => {
    if (status === 'ready' && inputRef.current) {
      inputRef.current.focus();
    }
  }, [status]);

  const redeem = useCallback(async () => {
    if (status === 'submitting') return;
    if (code.length !== CODE_LENGTH) {
      setBannerKind('error');
      setBanner(`Enter the ${CODE_LENGTH}-digit code shown on your Viola desktop.`);
      return;
    }
    setStatus('submitting');
    setBanner('');
    try {
      const res = await fetch('/bootstrap/confirm', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pairing_session_id: sessionId, code }),
      });

      if (res.ok) {
        const body = await res.json();
        const data = (body && body.data) || {};
        // Reload into spoke mode now that the device holds a credential.
        enterSpokeMode(data.spoke_token);
        return;
      }

      const detail = await parseDetail(res);
      if (res.status === 410) {
        // Code/session expired — start a new one (new code on the desktop) and
        // carry the reason through, or the fresh request would wipe it.
        await requestSession('That code expired. A new code is now shown on your Viola desktop.');
        return;
      }
      if (res.status === 429) {
        setBannerKind('error');
        setBanner(detail || 'Too many incorrect tries. Request a new code on your desktop.');
        setSessionId('');
        setStatus('error');
        return;
      }
      if (res.status === 404) {
        await requestSession('That code is no longer active. A new code is now shown on your Viola desktop.');
        return;
      }
      // 403 wrong code (or other) — keep the session, let the user retry.
      setBannerKind('error');
      setBanner(detail || 'Incorrect code. Check the code on your Viola desktop and try again.');
      setCode('');
      setStatus('ready');
    } catch {
      setBannerKind('error');
      setBanner('Could not reach Viola. Check your Wi-Fi and try again.');
      setStatus('ready');
    }
  }, [code, sessionId, status, requestSession, enterSpokeMode]);

  const onSubmit = useCallback(
    (e) => {
      e.preventDefault();
      redeem();
    },
    [redeem],
  );

  const submitting = status === 'submitting';
  const canType = status === 'ready' || status === 'submitting';

  // A scan is mid-exchange: show the joining state, not a code prompt.
  if (claiming) {
    return (
      <div style={styles.container}>
        <div style={styles.card} data-testid="spoke-pairing-claiming">
          <h1 style={styles.title}>Connecting to Viola</h1>
          <p style={styles.subtitle}>Joining as {room}...</p>
        </div>
      </div>
    );
  }

  return (
    <div style={styles.container}>
      <div style={styles.card} data-testid="spoke-pairing-gate">
        <h1 style={styles.title}>Connect to Viola</h1>
        <p style={styles.subtitle}>
          Enter the {CODE_LENGTH}-digit code shown on your Viola desktop to join as a speaker.
        </p>

        {banner && (
          <div
            style={{
              ...styles.banner,
              ...(bannerKind === 'error' ? styles.bannerError : styles.bannerInfo),
            }}
            role="status"
            aria-live="polite"
            data-testid="spoke-pairing-banner"
          >
            {banner}
          </div>
        )}

        {status === 'requesting' && (
          <p style={styles.hint} data-testid="spoke-pairing-requesting">
            Asking Viola for a code...
          </p>
        )}

        <form onSubmit={onSubmit} autoComplete="off">
          <input
            ref={inputRef}
            value={code}
            onChange={(e) => setCode(normalizeCode(e.target.value))}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]*"
            maxLength={CODE_LENGTH}
            placeholder={'0'.repeat(CODE_LENGTH)}
            aria-label="Pairing code"
            disabled={!canType}
            style={styles.input}
            data-testid="spoke-pairing-input"
          />
          <button
            type="submit"
            disabled={!canType || code.length !== CODE_LENGTH}
            style={{
              ...styles.button,
              ...((!canType || code.length !== CODE_LENGTH) ? styles.buttonDisabled : {}),
            }}
            data-testid="spoke-pairing-submit"
          >
            {submitting ? 'Connecting...' : 'Connect'}
          </button>
        </form>

        <button
          type="button"
          onClick={requestSession}
          disabled={status === 'requesting' || submitting}
          style={styles.linkButton}
          data-testid="spoke-pairing-new-code"
        >
          Don&apos;t see a code? Show a new one
        </button>

        <p style={styles.footer}>Room: {room}</p>
      </div>
    </div>
  );
}

SpokePairingGate.propTypes = {
  room: PropTypes.string,
  ticket: PropTypes.string,
};

SpokePairingGate.defaultProps = {
  room: 'speaker',
  ticket: '',
};

const styles = {
  container: {
    display: 'flex',
    justifyContent: 'center',
    alignItems: 'center',
    minHeight: '100vh',
    backgroundColor: THEME.colors.bgVoid,
    color: THEME.colors.textPrimary,
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    padding: '40px 20px',
  },
  card: {
    backgroundColor: THEME.colors.bgCard,
    borderRadius: 16,
    padding: 32,
    width: '100%',
    maxWidth: 420,
    border: `1px solid ${THEME.colors.borderSubtle}`,
    textAlign: 'center',
  },
  title: {
    fontSize: 24,
    fontWeight: 600,
    margin: '0 0 6px 0',
    color: THEME.colors.textPrimary,
  },
  subtitle: {
    fontSize: 14,
    color: THEME.colors.textSecondary,
    margin: '0 0 24px 0',
    lineHeight: 1.5,
  },
  banner: {
    padding: '12px 14px',
    borderRadius: 10,
    marginBottom: 16,
    fontSize: 13,
    textAlign: 'left',
    lineHeight: 1.45,
  },
  bannerError: {
    backgroundColor: 'rgba(239,68,68,0.12)',
    border: '1px solid rgba(239,68,68,0.3)',
    color: THEME.colors.statusRed,
  },
  bannerInfo: {
    backgroundColor: 'rgba(255,255,255,0.06)',
    border: `1px solid ${THEME.colors.borderLight}`,
    color: THEME.colors.textSecondary,
  },
  input: {
    width: '100%',
    padding: '16px',
    fontSize: 28,
    letterSpacing: '0.4em',
    textAlign: 'center',
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    border: `1px solid ${THEME.colors.borderLight}`,
    borderRadius: 12,
    backgroundColor: THEME.colors.bgElevated,
    color: THEME.colors.textPrimary,
    outline: 'none',
    boxSizing: 'border-box',
    marginBottom: 16,
  },
  button: {
    width: '100%',
    padding: '14px 0',
    fontSize: 16,
    fontWeight: 600,
    border: 'none',
    borderRadius: 10,
    backgroundColor: THEME.colors.accent,
    color: '#fff',
    cursor: 'pointer',
  },
  buttonDisabled: {
    opacity: 0.6,
    cursor: 'not-allowed',
  },
  linkButton: {
    marginTop: 18,
    background: 'none',
    border: 'none',
    color: THEME.colors.textMuted,
    fontSize: 13,
    cursor: 'pointer',
    textDecoration: 'underline',
  },
  footer: {
    marginTop: 16,
    marginBottom: 0,
    fontSize: 12,
    color: THEME.colors.textMuted,
  },
};
