import { useState, useRef, useEffect, useCallback } from 'react';

/**
 * Hook for Screen Wake Lock API to prevent display sleep.
 * Auto-reacquires on visibility change (browser releases lock when tab hidden).
 *
 * @returns {{ isActive: boolean, isSupported: boolean, requestWakeLock: Function, releaseWakeLock: Function }}
 */
export function useWakeLock() {
  const [isActive, setIsActive] = useState(false);
  const sentinelRef = useRef(null);
  const wantedRef = useRef(false);

  const isSupported = typeof navigator !== 'undefined' && 'wakeLock' in navigator;

  /** @type {Function} Request a screen wake lock */
  const requestWakeLock = useCallback(async () => {
    if (!isSupported) return;
    wantedRef.current = true;
    try {
      const sentinel = await navigator.wakeLock.request('screen');
      sentinelRef.current = sentinel;
      setIsActive(true);

      sentinel.addEventListener('release', () => {
        setIsActive(false);
        sentinelRef.current = null;
      });
    } catch {
      // Wake lock request can fail if page is not visible or permission denied
      setIsActive(false);
    }
  }, [isSupported]);

  /** @type {Function} Release the screen wake lock */
  const releaseWakeLock = useCallback(async () => {
    wantedRef.current = false;
    if (sentinelRef.current) {
      try {
        await sentinelRef.current.release();
      } catch {
        // Already released
      }
      sentinelRef.current = null;
      setIsActive(false);
    }
  }, []);

  // Reacquire wake lock when page becomes visible again
  useEffect(() => {
    if (!isSupported) return;

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible' && wantedRef.current && !sentinelRef.current) {
        requestWakeLock();
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [isSupported, requestWakeLock]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (sentinelRef.current) {
        sentinelRef.current.release().catch(() => {});
        sentinelRef.current = null;
      }
    };
  }, []);

  return {
    isActive,
    isSupported,
    requestWakeLock,
    releaseWakeLock,
  };
}

export default useWakeLock;
