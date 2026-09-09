/**
 * SpokeSessionBoundary — keeps a paired speaker paired, and never leaves a
 * device staring at a dead screen (#4434).
 *
 * A spoke credential can stop being valid while the device still holds it: it
 * aged out, it went unused for long enough to be retired, or the user revoked
 * that one device from the desktop. Before this, the page would render the
 * speaker UI anyway and simply fail to connect, with nothing on screen to
 * explain it or any way to fix it.
 *
 * On mount this asks the hub one question — am I still paired? — and:
 *   - still paired: nothing changes, the speaker renders as before;
 *   - the hub renewed an ageing credential: the fresh token replaces the one in
 *     the address bar (the HttpOnly cookie was already updated), so a speaker
 *     in normal use rolls forward and never meets the expiry wall;
 *   - no longer paired: the pairing gate takes over, so re-joining is the same
 *     one scan or one code it was the first time.
 *
 * The check is deliberately fail-open: an unreachable hub or a mangled reply
 * leaves the speaker exactly as it was, because a network blip must not throw a
 * working room off the air.
 */

import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import SpokePairingGate from './SpokePairingGate';

export default function SpokeSessionBoundary({ room, spokeToken, children }) {
  // null = still asking. Anything but an explicit "no" keeps the speaker up.
  const [paired, setPaired] = useState(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const headers = spokeToken ? { 'X-Spoke-Token': spokeToken } : undefined;
        const res = await fetch('/bootstrap/spoke-session', {
          method: 'GET',
          credentials: 'include',
          headers,
        });
        if (cancelled || !res.ok) return;

        const body = await res.json();
        const data = (body && body.data) || {};
        if (cancelled) return;

        if (data.paired === false) {
          setPaired(false);
          return;
        }
        setPaired(true);

        if (data.spoke_token) {
          try {
            const url = new URL(window.location.href);
            url.searchParams.set('spoke_token', data.spoke_token);
            window.history.replaceState({}, '', url.toString());
          } catch {
            // Address-bar tidiness only — the renewed cookie is what counts.
          }
        }
      } catch {
        // Hub unreachable: leave the speaker running on what it already has.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [spokeToken]);

  if (paired === false) {
    return <SpokePairingGate room={room} />;
  }
  return children;
}

SpokeSessionBoundary.propTypes = {
  room: PropTypes.string,
  spokeToken: PropTypes.string,
  children: PropTypes.node,
};

SpokeSessionBoundary.defaultProps = {
  room: 'speaker',
  spokeToken: '',
  children: null,
};
