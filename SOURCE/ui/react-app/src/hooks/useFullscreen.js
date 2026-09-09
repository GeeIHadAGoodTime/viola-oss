import { useState, useEffect, useCallback } from 'react';

/**
 * Hook for managing fullscreen mode with cross-browser support.
 * Handles both standard and webkit-prefixed APIs.
 *
 * @returns {{ isFullscreen: boolean, isSupported: boolean, enterFullscreen: Function, exitFullscreen: Function, toggleFullscreen: Function }}
 */
export function useFullscreen() {
  const [isFullscreen, setIsFullscreen] = useState(false);

  const isSupported = typeof document !== 'undefined' && !!(
    document.documentElement.requestFullscreen ||
    document.documentElement.webkitRequestFullscreen
  );

  /** @type {Function} Enter fullscreen mode */
  const enterFullscreen = useCallback(() => {
    const el = document.documentElement;
    if (el.requestFullscreen) {
      el.requestFullscreen().catch(() => {});
    } else if (el.webkitRequestFullscreen) {
      el.webkitRequestFullscreen();
    }
  }, []);

  /** @type {Function} Exit fullscreen mode */
  const exitFullscreen = useCallback(() => {
    if (document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    } else if (document.webkitExitFullscreen) {
      document.webkitExitFullscreen();
    }
  }, []);

  /** @type {Function} Toggle fullscreen mode */
  const toggleFullscreen = useCallback(() => {
    const currentlyFullscreen = !!(
      document.fullscreenElement || document.webkitFullscreenElement
    );
    if (currentlyFullscreen) {
      exitFullscreen();
    } else {
      enterFullscreen();
    }
  }, [enterFullscreen, exitFullscreen]);

  // Listen for fullscreen changes
  useEffect(() => {
    if (!isSupported) return;

    const handleChange = () => {
      setIsFullscreen(!!(
        document.fullscreenElement || document.webkitFullscreenElement
      ));
    };

    document.addEventListener('fullscreenchange', handleChange);
    document.addEventListener('webkitfullscreenchange', handleChange);

    return () => {
      document.removeEventListener('fullscreenchange', handleChange);
      document.removeEventListener('webkitfullscreenchange', handleChange);
    };
  }, [isSupported]);

  return {
    isFullscreen,
    isSupported,
    enterFullscreen,
    exitFullscreen,
    toggleFullscreen,
  };
}

export default useFullscreen;
