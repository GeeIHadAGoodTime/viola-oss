import React, { useState, useEffect, useCallback, useMemo } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { authFetch } from '../hooks/useViolaApi';
import { generateQR } from '../utils/qrcode';
import { maskPairingUrl, hasMaskablePairingSecret } from '../utils/pairingUrl';
import { isFeatureHidden } from '../utils/featureSurface';
import DesktopUpsell from './DesktopUpsell';

const theme = THEME;

// SVG Icons
const Icons = {
  Copy: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
    </svg>
  ),
  Check: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  ),
  Wifi: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M5 12.55a11 11 0 0 1 14.08 0"/>
      <path d="M1.42 9a16 16 0 0 1 21.16 0"/>
      <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
      <line x1="12" y1="20" x2="12.01" y2="20"/>
    </svg>
  ),
};

// Render QR modules as SVG
function QRCodeSVG({ modules, size = 200 }) {
  if (!modules || modules.length === 0) return null;

  const count = modules.length;
  const cellSize = size / count;

  const paths = [];
  for (let r = 0; r < count; r++) {
    for (let c = 0; c < count; c++) {
      if (modules[r][c]) {
        paths.push(`M${c * cellSize},${r * cellSize}h${cellSize}v${cellSize}h-${cellSize}z`);
      }
    }
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ borderRadius: '12px', background: 'white', padding: '8px', boxSizing: 'content-box' }}
    >
      <path d={paths.join('')} fill="#000000" />
    </svg>
  );
}

// Lightweight inline spinner (no device-discovery framing).
function Spinner() {
  return (
    <svg width="28" height="28" viewBox="0 0 50 50" data-essential-motion="spin" style={{ animation: 'viola-spin 1s linear infinite' }}>
      <style>{`@keyframes viola-spin { to { transform: rotate(360deg); } }`}</style>
      <circle
        cx="25"
        cy="25"
        r="20"
        fill="none"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeDasharray="80"
        strokeDashoffset="60"
      />
    </svg>
  );
}

// Localhost detection — used to surface a Wi-Fi hint without hiding the QR.
function isLocalhostIp(ip) {
  if (!ip) return true;
  return ip === '127.0.0.1' || ip === 'localhost' || ip.startsWith('::1');
}

// Wait briefly for window.__VIOLA_API_KEY__ to appear. The desktop shell
// injects it via runJavaScript in _on_load_finished, which can race with
// React's first useEffect. Poll instead of failing closed.
async function waitForApiKey(timeoutMs = 3000, intervalMs = 100) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (typeof window !== 'undefined' && window.__VIOLA_API_KEY__) {
      return true;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return Boolean(typeof window !== 'undefined' && window.__VIOLA_API_KEY__);
}

/**
 * Panel for generating a connection URL + QR code for spoke devices.
 * Shown in the "Add Room" tab of the Rooms modal.
 *
 * Architecture (correct, restored 2026-04-28):
 *   - Always renders QR + pairing code on success, even if the hub only
 *     knows its localhost address. The user can still type the 4-letter
 *     pairing word at useviola.com/connect from another device on the
 *     same Wi-Fi network.
 *   - Auth (401) is the only true failure surface — addressable by the
 *     desktop reloading its API-key injection. We never show generic
 *     "couldn't find this device on your network" because the panel is
 *     not doing device discovery; it is publishing the hub's address.
 */
function normalizeInitialRoomName(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : 'new-room';
}

export default function ConnectSpeakerPanel({ initialRoomName = 'new-room' }) {
  // Pairing publishes the hub's own LAN address and hands out a short-lived
  // code a phone on the same Wi-Fi uses to reach it. A browser tab on
  // api.useviola.com has no hub and no LAN, and the `/v1/network/*` routes it
  // needs are LOCAL_ONLY (backend/cloud_route_manifest.py "network"), so on the
  // cloud SPA both effects below used to 404 forever — the pending-code poll
  // once every 2.5s for as long as the panel stayed open — and the panel
  // rendered its instructions with no link and no QR under them (#3553).
  const roomsHidden = isFeatureHidden('rooms');
  const [networkInfo, setNetworkInfo] = useState(null);
  const [roomName, setRoomName] = useState(() => normalizeInitialRoomName(initialRoomName));
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState(false);
  const [copied, setCopied] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  // The link carries a short-lived pairing code, so it is hidden by default:
  // this screen is photographed and screen-shared, and the QR alone is enough
  // to pair. "Show link" is there for reading it out to someone.
  const [linkRevealed, setLinkRevealed] = useState(false);
  // Active PIN a joining device must enter. The join flow is device-initiated:
  // when a phone/browser opens the join page (no spoke token) it asks the hub
  // for a short-lived code; the hub shows it here so the user can read it to
  // the joining device. Polled while the panel is open.
  const [joinCode, setJoinCode] = useState(null);

  useEffect(() => {
    setRoomName(normalizeInitialRoomName(initialRoomName));
  }, [initialRoomName]);

  useEffect(() => {
    if (roomsHidden) {
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    (async () => {
      // Wait briefly for the desktop shell to inject the API key. This
      // prevents an immediate 401 race when the panel mounts faster than
      // the QWebEngine runJavaScript injection completes.
      await waitForApiKey();

      let response;
      try {
        response = await authFetch('/v1/network/local-address');
      } catch {
        // Network/abort: we genuinely cannot reach the hub. The desktop
        // shell exposes a fallback (it always returns its own localhost
        // address), so this branch is rare. Fall through to a degraded
        // QR rendered from a synthesized localhost record.
        if (!cancelled) {
          setNetworkInfo({
            ip: 'localhost',
            port: Number(window.location.port) || 8756,
            spoke_url: `${window.location.protocol}//${window.location.hostname}:${
              window.location.port || 8756
            }/?room=`,
            pairing_code: '',
          });
          setLoading(false);
        }
        return;
      }

      if (cancelled) return;

      if (response.status === 401) {
        setAuthError(true);
        setLoading(false);
        return;
      }

      try {
        const json = await response.json();
        const data = json && json.data !== undefined && json.ok !== false ? json.data : json;
        // Even an empty/partial payload should not block the panel. Render
        // whatever we have — the desktop hub always knows at least its own
        // address so the QR code remains useful for same-Wi-Fi pairing.
        setNetworkInfo(data || {});
      } catch {
        setNetworkInfo({});
      }
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey, roomsHidden]);

  // Poll for a pending join PIN so the desktop displays the code a joining
  // device must enter. Read-only and auth-protected; safe to poll while open.
  useEffect(() => {
    if (roomsHidden) return undefined;
    let cancelled = false;
    let timer = null;

    const poll = async () => {
      try {
        const response = await authFetch('/v1/network/pair/pending');
        if (cancelled || !response.ok) {
          if (!cancelled && response && response.status === 401) {
            // Auth not ready yet — keep polling; the network-info effect
            // surfaces the real auth-error state.
          }
        } else {
          const json = await response.json();
          const data = json && json.data !== undefined && json.ok !== false ? json.data : json;
          const pending = (data && Array.isArray(data.pending)) ? data.pending : [];
          if (!cancelled) setJoinCode(pending.length > 0 ? pending[0] : null);
        }
      } catch {
        // Transient network/abort — leave the last known code in place.
      }
      if (!cancelled) {
        timer = setTimeout(poll, 2500);
      }
    };

    // Defer the first poll one interval so panel mount makes exactly one
    // request (the local-address lookup); the join code appears shortly after
    // and refreshes every interval thereafter.
    timer = setTimeout(poll, 2500);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [reloadKey, roomsHidden]);

  // The pairing code inside the link expires in minutes (that is the point).
  // Re-fetch a fresh one shortly before it lapses so the QR on screen is
  // always scannable for as long as the panel is open, and re-hide the link
  // because the code just changed.
  useEffect(() => {
    const ttl = Number(networkInfo?.pairing_ticket_expires_in);
    if (!networkInfo || !Number.isFinite(ttl) || ttl <= 0) return undefined;
    const refreshInMs = Math.max(ttl - 45, 30) * 1000;
    const timer = setTimeout(() => {
      setLinkRevealed(false);
      setReloadKey((k) => k + 1);
    }, refreshInMs);
    return () => clearTimeout(timer);
  }, [networkInfo]);

  const spokeUrl = useMemo(() => {
    if (!networkInfo) return '';
    const safeName = roomName.trim().replace(/\s+/g, '-').toLowerCase() || 'new-room';
    const base = networkInfo.spoke_url
      || (networkInfo.ip && networkInfo.port
        ? `http://${networkInfo.ip}:${networkInfo.port}/?room=`
        : '');
    if (!base) return '';
    return `${base}${encodeURIComponent(safeName)}`;
  }, [networkInfo, roomName]);

  const qrModules = useMemo(() => {
    if (!spokeUrl) return null;
    try {
      return generateQR(spokeUrl);
    } catch {
      return null;
    }
  }, [spokeUrl]);

  const handleCopy = useCallback(async () => {
    if (!spokeUrl) return;
    try {
      await navigator.clipboard.writeText(spokeUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for non-HTTPS contexts
      const input = document.createElement('input');
      input.value = spokeUrl;
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      document.body.removeChild(input);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [spokeUrl]);

  if (roomsHidden) {
    return (
      <div
        style={{ display: 'flex', justifyContent: 'center', padding: '24px 8px' }}
        data-testid="connect-speaker-desktop-only"
      >
        <DesktopUpsell feature="rooms" />
      </div>
    );
  }

  if (loading) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: '12px',
          color: theme.colors.textMuted,
          padding: '40px',
        }}
        data-testid="connect-speaker-loading"
      >
        <div style={{ color: theme.colors.accent }}>
          <Spinner />
        </div>
        <div style={{ fontSize: '13px' }}>Preparing your room code...</div>
      </div>
    );
  }

  if (authError) {
    return (
      <div style={{ textAlign: 'center', padding: '40px 20px' }} data-testid="connect-speaker-auth-error">
        <div style={{ color: theme.colors.statusRed, fontSize: '14px', marginBottom: '8px' }}>
          The desktop app needs to refresh its sign-in.
        </div>
        <div
          style={{
            color: theme.colors.textMuted,
            fontSize: '13px',
            marginBottom: '16px',
            lineHeight: '1.5',
          }}
        >
          Restart Viola or click retry to try again.
        </div>
        <button
          onClick={() => {
            setAuthError(false);
            setLoading(true);
            setReloadKey((k) => k + 1);
          }}
          style={{
            padding: '10px 20px',
            borderRadius: '10px',
            border: 'none',
            backgroundColor: theme.colors.accent,
            color: 'white',
            fontSize: '14px',
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          Retry
        </button>
      </div>
    );
  }

  const showLocalhostHint = isLocalhostIp(networkInfo?.ip);

  return (
    <div
      style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '20px' }}
      data-testid="connect-speaker-ready"
    >
      {/* Instructions */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        color: theme.colors.textSecondary,
        fontSize: '14px',
        textAlign: 'center',
        lineHeight: '1.5',
      }}>
        <div style={{ color: theme.colors.accent, flexShrink: 0 }}>
          <Icons.Wifi />
        </div>
        <span>Open this link on another device to add it as a room.</span>
      </div>

      {/* Room name input */}
      <div style={{ width: '100%', maxWidth: '320px' }}>
        <label style={{
          display: 'block',
          marginBottom: '8px',
          color: theme.colors.textSecondary,
          fontSize: '13px',
        }}>
          Room name
        </label>
        <input
          type="text"
          value={roomName}
          onChange={(e) => setRoomName(e.target.value)}
          placeholder="e.g. living-room, kitchen"
          style={{
            width: '100%',
            padding: '12px 16px',
            borderRadius: '12px',
            border: `1px solid ${theme.colors.borderLight}`,
            backgroundColor: theme.colors.bgCard,
            color: theme.colors.textPrimary,
            fontSize: '14px',
            outline: 'none',
            boxSizing: 'border-box',
          }}
        />
      </div>

      {/* Active join PIN — shown when a device is waiting to be let in. The
          user reads this code to the joining device, which enters it to earn a
          spoke credential. */}
      {joinCode?.code && (
        <div
          style={{
            width: '100%',
            padding: '16px',
            borderRadius: '12px',
            backgroundColor: theme.colors.accentSubtle,
            border: `1px solid ${theme.colors.accentBorder}`,
            textAlign: 'center',
          }}
          data-testid="connect-speaker-join-code"
        >
          <div style={{ color: theme.colors.textSecondary, fontSize: '13px', marginBottom: '8px' }}>
            A device is connecting. Enter this code on it:
          </div>
          <div
            style={{
              color: theme.colors.textPrimary,
              fontSize: '34px',
              fontWeight: 700,
              fontFamily: 'monospace',
              letterSpacing: '8px',
            }}
            data-testid="connect-speaker-join-code-value"
          >
            {joinCode.code}
          </div>
          <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '6px' }}>
            This code expires shortly.
          </div>
        </div>
      )}

      {/* QR Code */}
      {qrModules && (
        <div style={{ padding: '4px' }} data-testid="connect-speaker-qr">
          <QRCodeSVG modules={qrModules} size={180} />
        </div>
      )}

      {/* Pairing code — cameraless path */}
      {networkInfo?.pairing_code && (
        <div style={{
          width: '100%',
          padding: '14px 16px',
          borderRadius: '12px',
          backgroundColor: theme.colors.bgCard,
          border: `1px dashed ${theme.colors.borderLight}`,
          textAlign: 'center',
        }}>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '12px',
            marginBottom: '6px',
            letterSpacing: '0.3px',
          }}>
            No camera? Go to <strong>useviola.com/connect</strong> and type:
          </div>
          <div
            style={{
              color: theme.colors.textPrimary,
              fontSize: '26px',
              fontWeight: 600,
              fontFamily: 'monospace',
              letterSpacing: '2px',
            }}
            data-testid="connect-speaker-pairing-code"
          >
            {networkInfo.pairing_code}
          </div>
        </div>
      )}

      {/* URL display — the pairing code inside the link stays hidden unless
          the user asks for it. This screen gets photographed and screen-shared,
          and scanning the QR does not need the text. */}
      {spokeUrl && (
        <div style={{ width: '100%' }}>
          <div
            style={{
              width: '100%',
              padding: '12px 16px',
              borderRadius: '12px',
              backgroundColor: theme.colors.bgElevated,
              border: `1px solid ${theme.colors.borderSubtle}`,
              wordBreak: 'break-all',
              color: theme.colors.textPrimary,
              fontSize: '13px',
              fontFamily: 'monospace',
              lineHeight: '1.5',
              textAlign: 'center',
              boxSizing: 'border-box',
            }}
            data-testid="connect-speaker-url"
          >
            {linkRevealed ? spokeUrl : maskPairingUrl(spokeUrl)}
          </div>
          {hasMaskablePairingSecret(spokeUrl) && (
            <div style={{ textAlign: 'center', marginTop: '8px' }}>
              <button
                type="button"
                onClick={() => setLinkRevealed((shown) => !shown)}
                style={{
                  background: 'none',
                  border: 'none',
                  color: theme.colors.textMuted,
                  fontSize: '12px',
                  cursor: 'pointer',
                  textDecoration: 'underline',
                  padding: 0,
                }}
                data-testid="connect-speaker-url-reveal"
              >
                {linkRevealed ? 'Hide link' : 'Show link'}
              </button>
              <div style={{ color: theme.colors.textMuted, fontSize: '11px', marginTop: '6px' }}>
                The code in this link expires in a few minutes and works once.
              </div>
            </div>
          )}
        </div>
      )}

      {/* Localhost hint — visible only when no LAN IP is known. We still
          render the QR + code above so the user can pair from a phone on
          the same Wi-Fi network. */}
      {showLocalhostHint && (
        <div
          style={{
            color: theme.colors.textMuted,
            fontSize: '12px',
            textAlign: 'center',
            lineHeight: '1.5',
            maxWidth: '320px',
          }}
          data-testid="connect-speaker-localhost-hint"
        >
          Make sure both devices are on the same Wi-Fi network.
        </div>
      )}

      {/* Copy button */}
      {spokeUrl && (
        <button
          onClick={handleCopy}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            padding: '10px 20px',
            borderRadius: '10px',
            border: 'none',
            backgroundColor: copied ? theme.colors.statusGreen + '20' : theme.colors.accent,
            color: copied ? theme.colors.statusGreen : 'white',
            fontSize: '14px',
            fontWeight: 500,
            cursor: 'pointer',
            transition: 'all 0.15s ease',
          }}
        >
          {copied ? <Icons.Check /> : <Icons.Copy />}
          {copied ? 'Copied' : 'Copy link'}
        </button>
      )}
    </div>
  );
}

ConnectSpeakerPanel.propTypes = {
  initialRoomName: PropTypes.string,
};
