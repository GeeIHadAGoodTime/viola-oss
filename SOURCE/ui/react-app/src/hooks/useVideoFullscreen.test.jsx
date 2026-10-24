import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  useVideoFullscreen,
  VIDEO_FULLSCREEN_DESKTOP_ONLY,
  VIDEO_FULLSCREEN_NOTHING_PLAYING,
} from './useVideoFullscreen';
import { dispatchUiActions } from '../utils/uiActions';

function uiAction(action) {
  act(() => {
    window.dispatchEvent(new CustomEvent('viola:ui-action', { detail: { action, payload: {} } }));
  });
}

function windowFullscreen(detail) {
  act(() => {
    window.dispatchEvent(new CustomEvent('viola:window-fullscreen', { detail }));
  });
}

function setup(overrides = {}) {
  const saved = { displayMode: 'chat', browserModeActive: false, calendarModeActive: false, displayPriorityOverride: null };
  const props = {
    isSpoke: false,
    hasVideo: true,
    modalOpen: false,
    stageMode: 'music',
    captureStage: vi.fn(() => saved),
    showMusicStage: vi.fn(),
    restoreStage: vi.fn(),
    notify: vi.fn(),
    ...overrides,
  };
  const hook = renderHook((p) => useVideoFullscreen(p), { initialProps: props });
  return { ...hook, props, saved };
}

describe('useVideoFullscreen', () => {
  let bridgeCalls;

  beforeEach(() => {
    bridgeCalls = [];
    window.viola = { setVideoFullscreen: vi.fn((enabled) => bridgeCalls.push(enabled)) };
  });

  afterEach(() => {
    delete window.viola;
  });

  it('enters on the voice ui_action and asks the Qt window to go full screen', () => {
    const { result, props } = setup();

    uiAction('enter_video_fullscreen');

    expect(result.current.videoFullscreen).toBe(true);
    expect(bridgeCalls).toEqual([true]);
    // Already on Music: nothing to switch or remember.
    expect(props.captureStage).not.toHaveBeenCalled();
    expect(props.showMusicStage).not.toHaveBeenCalled();
  });

  it('leaving puts the window back and does not touch the stage it never moved', () => {
    const { result, props } = setup();
    uiAction('enter_video_fullscreen');

    uiAction('exit_video_fullscreen');

    expect(result.current.videoFullscreen).toBe(false);
    expect(bridgeCalls).toEqual([true, false]);
    expect(props.restoreStage).not.toHaveBeenCalled();
  });

  it('switches from another stage to Music and restores that exact stage afterwards', () => {
    const { result, props, saved } = setup({ stageMode: 'chat' });

    uiAction('enter_video_fullscreen');
    expect(props.captureStage).toHaveBeenCalledTimes(1);
    expect(props.showMusicStage).toHaveBeenCalledTimes(1);
    expect(result.current.videoFullscreen).toBe(true);

    uiAction('exit_video_fullscreen');
    expect(props.restoreStage).toHaveBeenCalledWith(saved);
    expect(result.current.videoFullscreen).toBe(false);
  });

  it('the same reply delivered twice (response + broadcast) enters once and leaves once', () => {
    const { result, props } = setup({ stageMode: 'chat' });

    uiAction('enter_video_fullscreen');
    uiAction('enter_video_fullscreen');
    expect(props.captureStage).toHaveBeenCalledTimes(1);
    expect(result.current.videoFullscreen).toBe(true);

    uiAction('exit_video_fullscreen');
    uiAction('exit_video_fullscreen');
    // A second "leave" right after the first must not also take the window
    // out of an F11 fullscreen the user had before asking.
    expect(bridgeCalls.filter((enabled) => enabled === false)).toHaveLength(1);
    expect(props.restoreStage).toHaveBeenCalledTimes(1);
  });

  it('the dispatcher path from a real command result reaches the hook', () => {
    const { result } = setup();

    act(() => {
      dispatchUiActions({
        ok: true,
        data: {
          ui_action: 'enter_video_fullscreen',
          command_results: { routed: { data: { ui_action: 'enter_video_fullscreen' } } },
        },
      });
    });

    expect(result.current.videoFullscreen).toBe(true);
    expect(bridgeCalls).toEqual([true]);
  });

  it('F11, Esc or the title-bar button leaving window fullscreen ends the video mode without a second bridge call', () => {
    const { result, props } = setup({ stageMode: 'chat' });
    uiAction('enter_video_fullscreen');
    windowFullscreen({ fullscreen: true, video_fullscreen: true });
    expect(result.current.videoFullscreen).toBe(true);

    windowFullscreen({ fullscreen: false, video_fullscreen: false });

    expect(result.current.videoFullscreen).toBe(false);
    expect(bridgeCalls).toEqual([true]);
    expect(props.restoreStage).toHaveBeenCalledTimes(1);
  });

  it('says full screen is desktop-only when there is no Qt bridge (web app, LAN browser)', () => {
    delete window.viola;
    const { result, props } = setup();

    uiAction('enter_video_fullscreen');

    expect(result.current.videoFullscreen).toBe(false);
    expect(props.notify).toHaveBeenCalledWith(VIDEO_FULLSCREEN_DESKTOP_ONLY);
  });

  it('says nothing is playing when there is no video on the stage', () => {
    const { result, props } = setup({ hasVideo: false });

    uiAction('enter_video_fullscreen');

    expect(result.current.videoFullscreen).toBe(false);
    expect(props.notify).toHaveBeenCalledWith(VIDEO_FULLSCREEN_NOTHING_PLAYING);
    expect(bridgeCalls).toEqual([]);
  });

  it('a speaker (spoke) never drives the hub window', () => {
    const { result, props } = setup({ isSpoke: true });

    uiAction('enter_video_fullscreen');
    uiAction('exit_video_fullscreen');

    expect(result.current.videoFullscreen).toBe(false);
    expect(bridgeCalls).toEqual([]);
    expect(props.notify).not.toHaveBeenCalled();
  });

  it('ends full screen when the video goes away or a panel opens over it', () => {
    const { result, rerender, props } = setup();
    uiAction('enter_video_fullscreen');

    rerender({ ...props, hasVideo: false });
    expect(result.current.videoFullscreen).toBe(false);
    expect(bridgeCalls).toEqual([true, false]);

    rerender({ ...props, hasVideo: true });
    uiAction('enter_video_fullscreen');
    rerender({ ...props, hasVideo: true, modalOpen: true });
    expect(result.current.videoFullscreen).toBe(false);
    expect(bridgeCalls).toEqual([true, false, true, false]);
  });

  it('"end full screen" after a plain F11 still asks the window to leave full screen', () => {
    setup();

    uiAction('exit_video_fullscreen');

    expect(bridgeCalls).toEqual([false]);
  });
});
