import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../../hooks/useViolaApi';

export const PAIRING_PHASES = {
  CHECKING: 'checking',
  UNLINKED: 'unlinked',
  PAIRING: 'pairing',
  LINKED: 'linked',
};

export const CHANNEL_PAIRING_CONFIGS = {
  telegram: {
    id: 'telegram',
    name: 'Telegram',
    statusPath: '/auth/telegram/status',
    linkTokenPath: '/auth/telegram/link-token',
    unlinkPath: '/auth/telegram/unlink',
    defaultExpiresInMinutes: 15,
    pollIntervalMs: 3000,
    getDeepLink: (data) => data?.deep_link || data?.url || '',
    getAccountLabel: (status) => {
      if (status?.telegram_username) return `@${status.telegram_username}`;
      if (status?.telegram_first_name) return status.telegram_first_name;
      return 'Telegram';
    },
  },
};

function getExpiryTimestamp(data, defaultMinutes) {
  const minutes = Number(data?.expires_in_minutes);
  const ttlMinutes = Number.isFinite(minutes) && minutes > 0 ? minutes : defaultMinutes;
  return Date.now() + ttlMinutes * 60 * 1000;
}

function secondsUntil(timestamp) {
  if (!timestamp) return 0;
  return Math.max(0, Math.ceil((timestamp - Date.now()) / 1000));
}

function createLinkToken(raw, deepLink) {
  return {
    ...raw,
    deepLink,
  };
}

export function useChannelPairing(channelConfig) {
  const config = useMemo(() => channelConfig, [channelConfig]);
  const [phase, setPhase] = useState(PAIRING_PHASES.CHECKING);
  const [status, setStatus] = useState(null);
  const [linkToken, setLinkToken] = useState(null);
  const [expiresAt, setExpiresAt] = useState(null);
  const [remainingSeconds, setRemainingSeconds] = useState(0);
  const [error, setError] = useState('');
  const [busyAction, setBusyAction] = useState(null);

  const mountedRef = useRef(false);
  const actionRef = useRef(null);

  const clearPairingToken = useCallback(() => {
    setLinkToken(null);
    setExpiresAt(null);
    setRemainingSeconds(0);
  }, []);

  const applyStatus = useCallback((nextStatus, { preservePairing = false } = {}) => {
    setStatus(nextStatus || {});
    if (nextStatus?.linked) {
      clearPairingToken();
      setError('');
      setPhase(PAIRING_PHASES.LINKED);
      return;
    }
    if (!preservePairing) {
      setPhase(PAIRING_PHASES.UNLINKED);
    }
  }, [clearPairingToken]);

  const checkStatus = useCallback(async ({ initial = false, preservePairing = false, silent = false } = {}) => {
    if (initial) {
      setPhase(PAIRING_PHASES.CHECKING);
      setError('');
    }

    try {
      const nextStatus = await apiFetch(config.statusPath);
      if (!mountedRef.current) return null;
      applyStatus(nextStatus, { preservePairing });
      return nextStatus;
    } catch {
      if (!mountedRef.current) return null;
      // The first-open probe fires with zero user action; if the status call
      // fails (e.g. not signed in yet, or a transient hiccup) we must NOT greet
      // the user with a red "Unable to check ..." banner on a tab they just
      // opened. Fail silently into the UNLINKED state — the connect affordance
      // is the correct first-open presentation. Only surface the error banner
      // for a user-initiated check (Connect / retry), never the auto-probe.
      if (initial) {
        setPhase(PAIRING_PHASES.UNLINKED);
        return null;
      }
      if (!silent) {
        setError(`Unable to check ${config.name} connection. Please try again.`);
      }
      return null;
    }
  }, [applyStatus, config.name, config.statusPath]);

  const startPairing = useCallback(async () => {
    if (actionRef.current) return false;

    actionRef.current = 'pairing';
    setBusyAction('pairing');
    setError('');

    try {
      const data = await apiFetch(config.linkTokenPath, { method: 'POST' });
      if (!mountedRef.current) return false;

      const deepLink = config.getDeepLink(data);
      if (!deepLink) {
        throw new Error('No link returned');
      }

      const nextExpiresAt = getExpiryTimestamp(data, config.defaultExpiresInMinutes);
      setLinkToken(createLinkToken(data, deepLink));
      setExpiresAt(nextExpiresAt);
      setRemainingSeconds(secondsUntil(nextExpiresAt));
      setPhase(PAIRING_PHASES.PAIRING);
      return true;
    } catch {
      if (mountedRef.current) {
        clearPairingToken();
        setPhase(PAIRING_PHASES.UNLINKED);
        setError(`Failed to generate ${config.name} link. Please try again.`);
      }
      return false;
    } finally {
      if (mountedRef.current) {
        setBusyAction(null);
      }
      actionRef.current = null;
    }
  }, [clearPairingToken, config]);

  const cancelPairing = useCallback(() => {
    clearPairingToken();
    setError('');
    setPhase(status?.linked ? PAIRING_PHASES.LINKED : PAIRING_PHASES.UNLINKED);
  }, [clearPairingToken, status?.linked]);

  const unlink = useCallback(async () => {
    if (actionRef.current) return false;

    actionRef.current = 'unlink';
    setBusyAction('unlink');
    setError('');

    try {
      await apiFetch(config.unlinkPath, { method: 'POST' });
      if (!mountedRef.current) return false;
      setStatus({ linked: false });
      clearPairingToken();
      setPhase(PAIRING_PHASES.UNLINKED);
      return true;
    } catch {
      if (mountedRef.current) {
        setError(`Could not disconnect ${config.name}. Please try again.`);
      }
      return false;
    } finally {
      if (mountedRef.current) {
        setBusyAction(null);
      }
      actionRef.current = null;
    }
  }, [clearPairingToken, config.name, config.unlinkPath]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      actionRef.current = null;
    };
  }, []);

  useEffect(() => {
    clearPairingToken();
    setStatus(null);
    setError('');
    void checkStatus({ initial: true });
  }, [checkStatus, clearPairingToken]);

  useEffect(() => {
    if (phase !== PAIRING_PHASES.PAIRING || !linkToken) {
      return undefined;
    }

    const pollId = window.setInterval(() => {
      void checkStatus({ preservePairing: true, silent: true });
    }, config.pollIntervalMs);

    return () => {
      window.clearInterval(pollId);
    };
  }, [checkStatus, config.pollIntervalMs, linkToken, phase]);

  useEffect(() => {
    if (phase !== PAIRING_PHASES.PAIRING || !expiresAt) {
      return undefined;
    }

    const tick = () => {
      const next = secondsUntil(expiresAt);
      setRemainingSeconds(next);
      if (next <= 0) {
        clearPairingToken();
        setPhase(PAIRING_PHASES.UNLINKED);
        setError(`${config.name} link expired. Generate a new QR code to try again.`);
      }
    };

    tick();
    const timerId = window.setInterval(tick, 1000);
    return () => {
      window.clearInterval(timerId);
    };
  }, [clearPairingToken, config.name, expiresAt, phase]);

  return {
    phase,
    status,
    linkToken,
    expiresAt,
    remainingSeconds,
    error,
    busyAction,
    isBusy: Boolean(busyAction),
    checkStatus,
    startPairing,
    cancelPairing,
    unlink,
  };
}

export default useChannelPairing;
