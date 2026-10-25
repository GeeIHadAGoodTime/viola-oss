import { useCallback, useEffect, useRef, useState } from 'react';
import {
  canSetDesktopVideoFullscreen,
  setDesktopVideoFullscreen,
} from '../utils/runtimeSurface';

export const VIDEO_FULLSCREEN_DESKTOP_ONLY = 'Full screen is only available in the Viola desktop app.';
export const VIDEO_FULLSCREEN_NOTHING_PLAYING = "Nothing is playing, so there's no video to show full screen.";

// One reply can reach the app twice (the command response and the
// chat_response broadcast). A second "leave" that close to the first is the
// same request, and must not also take the window out of an F11 fullscreen
// the user had before asking.
const DUPLICATE_LEAVE_MS = 2000;

/**
 * Voice "full screen" for the video on the Music stage (desktop app only).
 *
 * The backend's `video_fullscreen` tool returns an `enter_video_fullscreen`
 * or `exit_video_fullscreen` ui_action. Entering remembers the stage that was
 * showing, switches to Music, asks the Qt window to go full screen and lays
 * the video over the page (CSS keyed off the returned `videoFullscreen`).
 * Leaving undoes exactly that. F11, Esc and the title-bar button leave too:
 * the Qt window reports them as `viola:window-fullscreen` events.
 *
 * @param {object} options
 * @param {boolean} options.isSpoke - a speaker never drives the hub window.
 * @param {boolean} options.hasVideo - a YouTube video is on the Music stage.
 * @param {boolean} options.modalOpen - a panel opened over the page.
 * @param {string} options.stageMode - the stage showing now ('music', 'chat', ...).
 * @param {Function} options.captureStage - returns a snapshot of the stage state.
 * @param {Function} options.showMusicStage - switches the stage to Music.
 * @param {Function} options.restoreStage - puts a captured snapshot back.
 * @param {Function} options.notify - shows a short message to the user.
 * @returns {{ videoFullscreen: boolean, enterVideoFullscreen: Function, exitVideoFullscreen: Function }}
 */
export function useVideoFullscreen({
  isSpoke = false,
  hasVideo = false,
  modalOpen = false,
  stageMode = 'music',
  captureStage,
  showMusicStage,
  restoreStage,
  notify,
}) {
  const [videoFullscreen, setVideoFullscreen] = useState(false);
  const activeRef = useRef(false);
  const savedStageRef = useRef(null);
  const lastLeaveAtRef = useRef(0);
  const latest = useRef({});
  latest.current = {
    isSpoke, hasVideo, stageMode, captureStage, showMusicStage, restoreStage, notify,
  };

  const leave = useCallback(({ fromWindow = false } = {}) => {
    const wasActive = activeRef.current;
    if (!wasActive && !fromWindow && Date.now() - lastLeaveAtRef.current < DUPLICATE_LEAVE_MS) {
      return;
    }
    activeRef.current = false;
    setVideoFullscreen(false);
    const saved = savedStageRef.current;
    savedStageRef.current = null;
    if (wasActive && saved && latest.current.restoreStage) {
      latest.current.restoreStage(saved);
    }
    if (fromWindow) return;
    lastLeaveAtRef.current = Date.now();
    setDesktopVideoFullscreen(false);
  }, []);

  const enter = useCallback(() => {
    const current = latest.current;
    if (current.isSpoke) return false;
    if (!canSetDesktopVideoFullscreen()) {
      current.notify?.(VIDEO_FULLSCREEN_DESKTOP_ONLY);
      return false;
    }
    if (!current.hasVideo) {
      current.notify?.(VIDEO_FULLSCREEN_NOTHING_PLAYING);
      return false;
    }
    if (!activeRef.current) {
      savedStageRef.current = current.stageMode !== 'music' && current.captureStage
        ? current.captureStage()
        : null;
      if (savedStageRef.current) current.showMusicStage?.();
      activeRef.current = true;
      setVideoFullscreen(true);
    }
    setDesktopVideoFullscreen(true);
    return true;
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const handleUiAction = (event) => {
      const action = event.detail?.action;
      if (action === 'enter_video_fullscreen') {
        enter();
      } else if (action === 'exit_video_fullscreen' && !latest.current.isSpoke) {
        leave();
      }
    };
    const handleWindowFullscreen = (event) => {
      if (activeRef.current && !event.detail?.video_fullscreen) {
        leave({ fromWindow: true });
      }
    };
    window.addEventListener('viola:ui-action', handleUiAction);
    window.addEventListener('viola:window-fullscreen', handleWindowFullscreen);
    return () => {
      window.removeEventListener('viola:ui-action', handleUiAction);
      window.removeEventListener('viola:window-fullscreen', handleWindowFullscreen);
    };
  }, [enter, leave]);

  // The video ended or a panel opened over it: nothing left to show full
  // screen, so put everything back rather than keep an empty black screen.
  useEffect(() => {
    if (activeRef.current && (!hasVideo || modalOpen)) leave();
  }, [hasVideo, modalOpen, leave]);

  return {
    videoFullscreen,
    enterVideoFullscreen: enter,
    exitVideoFullscreen: leave,
  };
}

export default useVideoFullscreen;
