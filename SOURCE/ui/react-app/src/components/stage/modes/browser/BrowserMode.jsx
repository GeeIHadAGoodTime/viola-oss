import { useCallback, useEffect, useMemo, useRef } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../../../config';
import styles from './BrowserMode.module.css';

const DEFAULT_AGENT_TASK = {
  description: '',
  phase: '',
  status: '',
};
const EMPTY_COMMANDS = [];

// Human-readable stage messages for the dedicated agent-browser stream states.
// Shown in the waiting surface (instead of a permanent spinner) when the
// cloud stream can't render — so the user knows WHY, not just that it's stuck.
const STREAM_ERROR_MESSAGES = {
  no_agent_browser: 'Viola is not browsing right now.',
  plan_tier_required: 'Live browsing is available on a paid plan.',
  agent_browser_disabled: 'Live browsing is temporarily unavailable.',
  screencast_failed: 'The browser stream could not start. Try again.',
  // #1061: the browser can't distinguish WHY a WebSocket handshake was
  // rejected pre-accept (kill-switch / origin / auth / plan-tier all collapse
  // to the same opaque close) — an honest generic message beats guessing.
  stream_unavailable: 'Live browsing could not connect.',
};

function streamWaitingMessage(streamStatus, streamError) {
  if (streamError && streamError.code) {
    return STREAM_ERROR_MESSAGES[streamError.code]
      || streamError.message
      || 'The browser stream could not start.';
  }
  if (streamStatus === 'ended') return 'Viola finished browsing.';
  if (streamStatus === 'connecting' || streamStatus === 'idle') return 'Connecting to browser view';
  return 'Waiting for browser view';
}

function hostFromUrl(url) {
  if (!url) return 'about:blank';
  try {
    const parsed = new URL(url);
    return parsed.host || parsed.href;
  } catch {
    return url;
  }
}

function sendBrowserBoundsFor(element) {
  if (!element) return;
  if (!window.viola || typeof window.viola.setBrowserOverlayBounds !== 'function') return;
  const rect = element.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;
  window.viola.setBrowserOverlayBounds(
    Math.round(rect.x),
    Math.round(rect.y),
    Math.round(rect.width),
    Math.round(rect.height),
  );
}

export default function BrowserMode({
  browserUrl = '',
  agentTask = DEFAULT_AGENT_TASK,
  agentBusy = false,
  recentlyActive = false,
  takeoverActive = false,
  isSpoke = false,
  streamFrames = false,
  agentFrameSrc = null,
  agentFrameFade = null,
  wsSend = null,
  inputSend = null,
  streamStatus = null,
  streamError = null,
  onTakeoverChange = null,
  onExit = null,
  onPTTStart = null,
  onPTTEnd = null,
  commandRegistry = null,
  commandScopeActive = false,
}) {
  const surfaceRef = useRef(null);
  const wsSendRef = useRef(wsSend);
  // Input (agent_browser_input) is relayed through `inputSend` — the dedicated
  // /ws/agent-browser stream sender for cloud web clients — falling back to
  // `wsSend` (the shared /ws/events sender) for desktop/spoke compatibility.
  // Control actions (takeover/continue/cancel) always stay on `wsSend`.
  const effectiveInputSend = inputSend || wsSend;
  const inputSendRef = useRef(effectiveInputSend);
  const onTakeoverChangeRef = useRef(onTakeoverChange);
  const takeoverActiveRef = useRef(takeoverActive);
  // Render the agent's browser as streamed JPEG frames (and relay
  // mouse/keyboard over the WebSocket) for any client without a native
  // embedded webview: multiroom spokes AND cloud/LAN web clients.
  const streamMode = isSpoke || streamFrames;
  const host = useMemo(() => hostFromUrl(browserUrl), [browserUrl]);
  const phase = agentTask?.phase || '';
  const status = agentTask?.status || '';
  const description = agentTask?.description || '';

  const statusLabel = useMemo(() => {
    if (takeoverActive) return 'Your turn';
    if (status === 'payment_review') return 'Review needed';
    if (phase === 'thinking') return 'Viola is thinking';
    if (agentBusy) return 'Viola is browsing';
    if (recentlyActive) return 'Recent browser';
    return 'Browser';
  }, [agentBusy, phase, recentlyActive, status, takeoverActive]);

  useEffect(() => {
    wsSendRef.current = wsSend;
  }, [wsSend]);

  useEffect(() => {
    inputSendRef.current = effectiveInputSend;
  }, [effectiveInputSend]);

  useEffect(() => {
    onTakeoverChangeRef.current = onTakeoverChange;
  }, [onTakeoverChange]);

  useEffect(() => {
    takeoverActiveRef.current = takeoverActive;
  }, [takeoverActive]);

  const requestTakeover = useCallback(() => {
    if (takeoverActiveRef.current) return;
    if (wsSendRef.current) wsSendRef.current({ action: 'agent_takeover', payload: {} });
    if (onTakeoverChangeRef.current) onTakeoverChangeRef.current(true);
  }, []);

  const releaseTakeover = useCallback(() => {
    if (!takeoverActiveRef.current) return;
    if (wsSendRef.current) wsSendRef.current({ action: 'agent_continue', payload: {} });
    if (onTakeoverChangeRef.current) onTakeoverChangeRef.current(false);
  }, []);

  useEffect(() => {
    const target = surfaceRef.current;
    const sendBounds = () => sendBrowserBoundsFor(target);
    const onBridgeReady = () => sendBounds();

    window.addEventListener('viola-bridge-ready', onBridgeReady);
    window.addEventListener('resize', sendBounds);

    let observer = null;
    if (target && typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(sendBounds);
      observer.observe(target);
    }

    sendBounds();
    return () => {
      window.removeEventListener('viola-bridge-ready', onBridgeReady);
      window.removeEventListener('resize', sendBounds);
      if (observer) observer.disconnect();
    };
  }, []);

  const sendTakeover = useCallback(() => {
    if (takeoverActive) {
      releaseTakeover();
      return;
    }
    requestTakeover();
  }, [releaseTakeover, requestTakeover, takeoverActive]);

  const sendCancel = useCallback(() => {
    if (wsSendRef.current) wsSendRef.current({ action: 'agent_cancel', payload: {} });
  }, []);

  const registerCommands = commandRegistry?.registerCommands;
  const activeBrowserCommands = useMemo(() => {
    const commands = [];
    if (takeoverActive) {
      commands.push({
        id: 'browser.hand-back',
        label: 'Hand back to agent',
        group: 'Browser',
        keywords: ['continue', 'release', 'viola'],
        perform: releaseTakeover,
      });
    } else {
      commands.push({
        id: 'browser.take-over',
        label: 'Take over browser',
        group: 'Browser',
        keywords: ['control', 'manual', 'handoff'],
        perform: requestTakeover,
      });
    }
    if (agentBusy) {
      commands.push({
        id: 'browser.stop-agent',
        label: 'Stop agent',
        group: 'Browser',
        keywords: ['cancel', 'task', 'browser'],
        perform: sendCancel,
      });
    }
    return commands;
  }, [
    agentBusy,
    releaseTakeover,
    requestTakeover,
    sendCancel,
    takeoverActive,
  ]);
  const registeredBrowserCommands = commandScopeActive ? activeBrowserCommands : EMPTY_COMMANDS;

  useEffect(() => {
    if (typeof registerCommands !== 'function') return undefined;
    return registerCommands('stage.mode.browser.context', registeredBrowserCommands);
  }, [registeredBrowserCommands, registerCommands]);

  // Relay a click on the streamed frame back to the agent's browser as a
  // resolution-independent ratio. Used by spokes and cloud web clients.
  // Routed through `inputSend` (dedicated /ws/agent-browser for cloud web
  // clients) which falls back to `wsSend` for desktop/spoke.
  const relayStreamClick = useCallback((event) => {
    if (!streamMode || !inputSendRef.current) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    inputSendRef.current({
      action: 'agent_browser_input',
      payload: {
        type: 'mouse_click',
        x_ratio: (event.clientX - rect.left) / rect.width,
        y_ratio: (event.clientY - rect.top) / rect.height,
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
    });
  }, [streamMode]);

  // Relay keyboard input on the streamed frame so the user can type during
  // takeover. Printable characters go as `text_input`; named keys
  // (Enter, Tab, Backspace, arrows, etc.) and modifier combos go as
  // `key_press` over the same `agent_browser_input` WS action.
  const relayStreamKeyDown = useCallback((event) => {
    if (!streamMode || !inputSendRef.current) return;
    const { key } = event;
    if (!key) return;
    // A single printable character with no control modifier is text entry.
    const printable = key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;
    if (printable) {
      event.preventDefault();
      inputSendRef.current({
        action: 'agent_browser_input',
        payload: { type: 'text_input', text: key },
      });
      return;
    }
    // Named key or modified combo — relay as a key press.
    event.preventDefault();
    inputSendRef.current({
      action: 'agent_browser_input',
      payload: {
        type: 'key_press',
        key,
        ctrl: event.ctrlKey,
        meta: event.metaKey,
        alt: event.altKey,
        shift: event.shiftKey,
      },
    });
  }, [streamMode]);

  return (
    <section className={styles.container} data-testid="browser-mode">
      <div className={styles.topBar}>
        <div className={styles.urlPill} style={{ borderColor: THEME.colors.borderLight }}>
          <span className={styles.lockIcon} aria-hidden="true">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="3" y="11" width="18" height="10" rx="2" />
              <path d="M7 11V8a5 5 0 0 1 10 0v3" />
            </svg>
          </span>
          <span className={styles.urlText}>{host}</span>
        </div>

        <div className={styles.activityPill} data-active={agentBusy || takeoverActive || recentlyActive}>
          <span className={phase === 'thinking' ? styles.spinner : styles.pulseDot} />
          <span>{statusLabel}</span>
        </div>

        <div className={styles.controls}>
          <button
            type="button"
            className={styles.controlButton}
            onClick={sendTakeover}
            aria-pressed={takeoverActive}
            aria-label={takeoverActive ? 'Hand browser back to Viola' : 'Take over browser'}
            title={takeoverActive ? 'Hand back' : 'Take over'}
          >
            {takeoverActive ? (
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M5 12h14" />
                <path d="m13 6 6 6-6 6" />
              </svg>
            ) : (
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M18 11V6a3 3 0 0 0-6 0v5" />
                <path d="M12 11V5a3 3 0 0 0-6 0v9a6 6 0 0 0 12 0v-3" />
              </svg>
            )}
          </button>

          {agentBusy && (
            <button
              type="button"
              className={styles.controlButton}
              onClick={sendCancel}
              aria-label="Stop agent task"
              title="Stop"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M18 6 6 18" />
                <path d="m6 6 12 12" />
              </svg>
            </button>
          )}

          {onPTTStart && onPTTEnd && (
            <button
              type="button"
              className={styles.controlButton}
              onMouseDown={onPTTStart}
              onMouseUp={onPTTEnd}
              onMouseLeave={onPTTEnd}
              aria-label="Push to talk"
              title="Push to talk"
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <rect x="9" y="2" width="6" height="12" rx="3" />
                <path d="M5 10a7 7 0 0 0 14 0" />
                <path d="M12 17v5" />
                <path d="M8 22h8" />
              </svg>
            </button>
          )}

          {onExit && (
            <button
              type="button"
              className={styles.controlButton}
              onClick={onExit}
              aria-label="Exit browser mode"
              title="Close"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M18 6 6 18" />
                <path d="m6 6 12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      <div
        ref={surfaceRef}
        data-testid="browser-surface"
        className={`${styles.surface} ${streamMode ? styles.streamSurface : ''}`}
        onClick={relayStreamClick}
        onKeyDown={streamMode ? relayStreamKeyDown : undefined}
        role={streamMode ? 'application' : undefined}
        aria-label={streamMode ? 'Agent browser stream' : undefined}
        tabIndex={streamMode ? 0 : undefined}
      >
        {streamMode && agentFrameFade && (
          <img src={agentFrameFade} alt="" className={`${styles.frame} ${styles.frameFade}`} />
        )}
        {streamMode && agentFrameSrc && (
          <img src={agentFrameSrc} alt="Agent browser view" className={`${styles.frame} ${styles.frameCurrent}`} />
        )}
        {streamMode && !agentFrameSrc && (
          <div
            className={styles.waitingState}
            data-stream-status={streamStatus || undefined}
            data-stream-error={streamError?.code || undefined}
          >
            {streamWaitingMessage(streamStatus, streamError)}
          </div>
        )}
      </div>

      {takeoverActive && (
        <div className={styles.takeoverBar}>
          <span>{description || 'The browser is yours'}</span>
          <button type="button" onClick={sendTakeover}>Hand back</button>
        </div>
      )}
    </section>
  );
}

BrowserMode.propTypes = {
  browserUrl: PropTypes.string,
  agentTask: PropTypes.shape({
    description: PropTypes.string,
    phase: PropTypes.string,
    status: PropTypes.string,
  }),
  agentBusy: PropTypes.bool,
  recentlyActive: PropTypes.bool,
  takeoverActive: PropTypes.bool,
  isSpoke: PropTypes.bool,
  streamFrames: PropTypes.bool,
  agentFrameSrc: PropTypes.string,
  agentFrameFade: PropTypes.string,
  wsSend: PropTypes.func,
  inputSend: PropTypes.func,
  streamStatus: PropTypes.string,
  streamError: PropTypes.shape({
    code: PropTypes.string,
    message: PropTypes.string,
  }),
  onTakeoverChange: PropTypes.func,
  onExit: PropTypes.func,
  onPTTStart: PropTypes.func,
  onPTTEnd: PropTypes.func,
  commandRegistry: PropTypes.shape({
    registerCommands: PropTypes.func.isRequired,
  }),
  commandScopeActive: PropTypes.bool,
};
