/**
 * SpokeWrapper — Wraps SmartDisplay for browser spoke mode.
 *
 * When ?room= is present in the URL, App.jsx renders this component.
 * It provides:
 *  1. Connect gate — user must tap "Connect as Speaker" before audio starts
 *  2. Speaker name input (persisted to localStorage)
 *  3. PCM audio stream from the hub via WebSocket
 *  4. Fullscreen toggle overlay button (hidden on iOS, PWA tip shown instead)
 *  5. Screen wake lock (keeps display on)
 *  6. PWA install prompt (Android programmatic, iOS manual instruction)
 *
 * SmartDisplay renders identically to the hub. This wrapper only adds
 * browser-specific overlays on top — no layout changes to SmartDisplay.
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import PropTypes from 'prop-types';
import SmartDisplay from '../SmartDisplay';
import { AuthProvider } from '../hooks/useAuth';
import { useFullscreen } from '../hooks/useFullscreen';
import { useVoiceStream } from '../hooks/useVoiceStream';
import { useWakeLock } from '../hooks/useWakeLock';
import { WS_URL, THEME } from '../config';
import { SpokeAudioEngine } from '../utils/spokeAudioEngine';
import { prewarmTtsContext } from '../utils/ttsPlayback';

/**
 * Detect whether the device is in standalone/PWA mode (installed to home screen).
 * Uses standard matchMedia query — works on all platforms, no UA sniffing.
 * @returns {boolean}
 */
function isStandalone() {
  if (typeof window === 'undefined') return false;
  return window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true; // Safari-specific but feature-detected
}

function buildSpokeAudioUrl(room) {
  const params = new URLSearchParams();
  if (typeof window !== 'undefined') {
    const currentParams = new URLSearchParams(window.location.search);
    const spokeToken = currentParams.get('spoke_token');
    if (spokeToken) params.set('spoke_token', spokeToken);
  }
  if (room) params.set('room', room);
  const qs = params.toString();
  return `${WS_URL}/ws/audio-stream${qs ? `?${qs}` : ''}`;
}

/**
 * Fullscreen toggle button — semi-transparent overlay, top-right corner.
 */
function FullscreenButton({ isFullscreen, onToggle }) {
  return (
    <button
      onClick={onToggle}
      title={isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'}
      aria-label={isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'}
      style={{
        position: 'fixed',
        top: '12px',
        right: '12px',
        zIndex: 9999,
        width: '36px',
        height: '36px',
        borderRadius: '8px',
        border: 'none',
        backgroundColor: 'rgba(0,0,0,0.4)',
        color: 'rgba(255,255,255,0.7)',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        transition: 'opacity 0.2s ease',
        opacity: 0.5,
      }}
      onMouseOver={e => { e.currentTarget.style.opacity = '1'; }}
      onMouseOut={e => { e.currentTarget.style.opacity = '0.5'; }}
    >
      {isFullscreen ? (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M4 14h6v6" />
          <path d="M20 10h-6V4" />
          <path d="M14 10l7-7" />
          <path d="M3 21l7-7" />
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M8 3H5a2 2 0 0 0-2 2v3" />
          <path d="M21 8V5a2 2 0 0 0-2-2h-3" />
          <path d="M3 16v3a2 2 0 0 0 2 2h3" />
          <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
        </svg>
      )}
    </button>
  );
}

FullscreenButton.propTypes = {
  isFullscreen: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
};

/**
 * iOS install instructions banner — shown at bottom of screen.
 */
function IOSInstallBanner({ onDismiss }) {
  return (
    <div style={{
      position: 'fixed',
      bottom: 0,
      left: 0,
      right: 0,
      backgroundColor: THEME.colors.bgElevated,
      borderTop: `1px solid ${THEME.colors.borderLight}`,
      padding: '16px',
      display: 'flex',
      alignItems: 'center',
      gap: '12px',
      zIndex: 9999,
    }}>
      <div style={{ flex: 1, fontSize: '13px', color: THEME.colors.textSecondary, lineHeight: '1.4' }}>
        For fullscreen, tap Share then &quot;Add to Home Screen&quot;.
      </div>
      <button
        onClick={onDismiss}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: '28px',
          height: '28px',
          borderRadius: '6px',
          border: 'none',
          backgroundColor: 'transparent',
          color: THEME.colors.textMuted,
          cursor: 'pointer',
          flexShrink: 0,
        }}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
  );
}

IOSInstallBanner.propTypes = {
  onDismiss: PropTypes.func.isRequired,
};

/**
 * Microphone toggle button — bottom-left overlay when connected.
 * Controls continuous voice streaming to hub for wake word detection.
 * Default OFF (privacy opt-in). Preference persisted in localStorage.
 */
function MicToggle({ isStreaming, isSupported, onToggle }) {
  if (!isSupported) return null;

  return (
    <button
      onClick={onToggle}
      title={isStreaming ? 'Disable wake word mic' : 'Enable wake word mic'}
      aria-label={isStreaming ? 'Disable wake word mic' : 'Enable wake word mic'}
      style={{
        position: 'fixed',
        bottom: '16px',
        left: '16px',
        zIndex: 9999,
        width: '44px',
        height: '44px',
        borderRadius: '50%',
        border: `2px solid ${isStreaming ? THEME.colors.accent : 'rgba(255,255,255,0.2)'}`,
        backgroundColor: isStreaming ? 'rgba(239,68,68,0.15)' : 'rgba(0,0,0,0.5)',
        color: isStreaming ? THEME.colors.accent : 'rgba(255,255,255,0.5)',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        transition: 'all 0.2s ease',
      }}
    >
      {/* Mic icon */}
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
        <line x1="12" y1="19" x2="12" y2="23" />
        <line x1="8" y1="23" x2="16" y2="23" />
      </svg>
      {/* Slash overlay when muted */}
      {!isStreaming && (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" style={{ position: 'absolute' }}>
          <line x1="2" y1="2" x2="22" y2="22" />
        </svg>
      )}
    </button>
  );
}

MicToggle.propTypes = {
  isStreaming: PropTypes.bool.isRequired,
  isSupported: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
};

/**
 * Floating banner that shows when wake word is detected.
 */
function WakeBanner({ text, visible }) {
  if (!visible) return null;
  return (
    <div style={{
      position: 'fixed',
      top: '50%',
      left: '50%',
      transform: 'translate(-50%, -50%)',
      zIndex: 10000,
      padding: '16px 32px',
      borderRadius: '12px',
      backgroundColor: 'rgba(0,0,0,0.85)',
      border: `1px solid ${THEME.colors.accent}`,
      color: '#fff',
      fontSize: '16px',
      fontWeight: 600,
      textAlign: 'center',
      pointerEvents: 'none',
      animation: 'fadeInScale 0.2s ease-out',
    }}>
      {text}
    </div>
  );
}

WakeBanner.propTypes = {
  text: PropTypes.string.isRequired,
  visible: PropTypes.bool.isRequired,
};

/**
 * Toast notification when spoke tab goes to background.
 */
function BackgroundToast({ visible, onDismiss }) {
  if (!visible) return null;
  return (
    <div style={{
      position: 'fixed',
      top: '16px',
      left: '50%',
      transform: 'translateX(-50%)',
      zIndex: 10001,
      padding: '12px 24px',
      borderRadius: '10px',
      backgroundColor: 'rgba(255,180,0,0.95)',
      color: '#1a1a1a',
      fontSize: '14px',
      fontWeight: 600,
      textAlign: 'center',
      boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
      display: 'flex',
      alignItems: 'center',
      gap: '10px',
      maxWidth: '90vw',
    }}>
      <span>Tab in background — audio paused. Return to tab to resume.</span>
      <button
        onClick={onDismiss}
        style={{
          background: 'none',
          border: 'none',
          color: '#1a1a1a',
          cursor: 'pointer',
          fontSize: '18px',
          lineHeight: 1,
          padding: '0 2px',
          flexShrink: 0,
        }}
      >
        ×
      </button>
    </div>
  );
}

BackgroundToast.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
};

/**
 * SpokeWrapper component.
 * @param {Object} props
 * @param {string} props.room - Room name from URL param
 */
export default function SpokeWrapper({ room }) {
  const engineRef = useRef(null);
  const micStreamRef = useRef(null);
  const [micStream, setMicStream] = useState(null);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);

  // Speaker name — persisted to localStorage, pre-filled from ?room= param
  const [speakerName, setSpeakerName] = useState(() => {
    if (typeof localStorage !== 'undefined') {
      const saved = localStorage.getItem('viola_room_name');
      if (saved) return saved;
    }
    // Default to the room param with first letter capitalized
    return room ? room.charAt(0).toUpperCase() + room.slice(1) : 'Speaker';
  });

  // Connect handler — called inside button click (user gesture), so
  // AudioContext starts 'running' immediately on both desktop and mobile.
  //
  // CRITICAL ORDER (iOS Safari): AudioContext MUST be created synchronously
  // in the user gesture's call stack BEFORE any async work (getUserMedia,
  // fetch, etc.). If getUserMedia runs first, it consumes the gesture
  // context and iOS permanently blocks the AudioContext in 'suspended'.
  //
  // Flow: click → engine.start() [creates AudioContext in gesture] → getUserMedia [mic]
  const handleConnect = useCallback(async () => {
    if (engineRef.current || connecting) return;
    setConnecting(true);

    // iOS Safari: pre-warm the TTS playback AudioContext while we still
    // have the user gesture. The shared TTS context is owned by
    // ttsPlayback.js; without this call iOS keeps it suspended when the
    // first WS-delivered TTS PCM arrives later and the audio plays
    // silently. Safe no-op on non-iOS.
    prewarmTtsContext();

    // Persist speaker name before engine starts (engine reads it at
    // registration time inside _runInitialSync).
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('viola_room_name', speakerName || room);
    }

    // 1. Start engine FIRST — creates and resumes AudioContext synchronously
    //    in this user gesture handler. This is the critical path for iOS.
    const wsUrl = buildSpokeAudioUrl(room);
    if (import.meta.env.DEV) {
      const hasSpokeToken = typeof window !== 'undefined'
        && new URLSearchParams(window.location.search).has('spoke_token');
      console.log('[SpokeWrapper] Starting audio stream', { room, hasSpokeToken });
    }
    const engine = new SpokeAudioEngine(wsUrl, () => {}, () => {});
    engineRef.current = engine;

    engine._onBackgroundChange = (isBackground) => {
      setShowBackgroundToast(isBackground);
    };

    try {
      await engine.start();
      // Wire gainNodeRef for wake-word ducking (was dead ref before this fix)
      gainNodeRef.current = engine._gainNode;
      setConnected(true);
    } catch (err) {
      console.warn('[SpokeWrapper] Engine start failed:', err);
      // Engine may still be usable (suspended AudioContext on some browsers)
      // — mark connected so user sees the display and can interact
      gainNodeRef.current = engine._gainNode;
      setConnected(true);
    } finally {
      setConnecting(false);
    }

    // Mic stream is NOT acquired here.  It is acquired on-demand by
    // useVoiceStream when the user enables the mic toggle.  Pre-acquiring
    // the stream kept iOS's VoiceProcessingIO (VPIO) active for the
    // entire session, which caused playback ducking on ANY ambient sound.
  }, [speakerName, room]);

  const handleNameChange = useCallback((e) => {
    const name = e.target.value;
    setSpeakerName(name);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('viola_room_name', name);
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (engineRef.current) {
        engineRef.current.stop();
        engineRef.current = null;
      }
      if (micStreamRef.current) {
        micStreamRef.current.getTracks().forEach(t => t.stop());
        micStreamRef.current = null;
        setMicStream(null);
      }
    };
  }, []);

  // Cleanup on page unload
  useEffect(() => {
    const onBeforeUnload = () => {
      if (engineRef.current) {
        engineRef.current.stop();
      }
      if (micStreamRef.current) {
        micStreamRef.current.getTracks().forEach(t => t.stop());
        micStreamRef.current = null;
        setMicStream(null);
      }
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, []);

  // 2. Fullscreen
  const { isFullscreen, isSupported: fullscreenSupported, toggleFullscreen } = useFullscreen();
  const alreadyInstalled = isStandalone();

  // 3. Wake lock — auto-request on mount
  const { requestWakeLock, isSupported: wakeLockSupported } = useWakeLock();
  useEffect(() => {
    if (wakeLockSupported) {
      requestWakeLock();
    }
  }, [wakeLockSupported, requestWakeLock]);

  // 4. PWA install prompt (Android / Chrome desktop)
  const [installPrompt, setInstallPrompt] = useState(null);
  const [showIOSInstall, setShowIOSInstall] = useState(false);

  useEffect(() => {
    const handler = (e) => {
      e.preventDefault();
      setInstallPrompt(e);
    };
    window.addEventListener('beforeinstallprompt', handler);
    return () => window.removeEventListener('beforeinstallprompt', handler);
  }, []);

  useEffect(() => {
    // Show install hint on devices that don't fire 'beforeinstallprompt'
    // (Safari/iOS) and aren't already installed as a PWA.
    if (!alreadyInstalled && !installPrompt) {
      setShowIOSInstall(true);
    }
  }, [alreadyInstalled, installPrompt]);

  const handleInstall = useCallback(async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    const result = await installPrompt.userChoice;
    if (result.outcome === 'accepted') {
      setInstallPrompt(null);
    }
  }, [installPrompt]);

  // 5. Voice stream for wake word detection (mic → hub)
  const [wakeBanner, setWakeBanner] = useState({ visible: false, text: '' });
  const [showBackgroundToast, setShowBackgroundToast] = useState(false);
  const gainNodeRef = useRef(null); // for audio ducking on wake

  const handleWakeDetected = useCallback((payload) => {
    // Show banner
    setWakeBanner({ visible: true, text: 'Listening...' });
    // Duck audio playback
    if (gainNodeRef.current) {
      gainNodeRef.current.gain.setTargetAtTime(0.1, gainNodeRef.current.context.currentTime, 0.05);
    }
  }, []);

  const handleCommandResult = useCallback((payload) => {
    const text = payload.transcript
      ? `"${payload.transcript}" — ${payload.response || ''}`
      : payload.response || '';
    setWakeBanner({ visible: true, text: text || 'Done' });
    // Restore audio volume — use engine's stored volume, not a hardcoded 1.0
    // (1.0 was 25% louder than the user's 80% setting after each voice command)
    if (gainNodeRef.current) {
      const targetGain = engineRef.current?._volumeBeforeMute ?? 0.8;
      gainNodeRef.current.gain.setTargetAtTime(targetGain, gainNodeRef.current.context.currentTime, 0.1);
    }
    // Fade banner after 3 seconds
    setTimeout(() => setWakeBanner({ visible: false, text: '' }), 3000);
  }, []);

  const handleStreamAcquired = useCallback((stream) => {
    micStreamRef.current = stream;
    setMicStream(stream);
  }, []);

  const {
    startStreaming,
    stopStreaming,
    isStreaming,
    isSupported: voiceStreamSupported,
  } = useVoiceStream({
    room,
    onWakeDetected: handleWakeDetected,
    onTranscription: handleCommandResult,
    existingStream: micStream,
    onStreamAcquired: handleStreamAcquired,
  });

  // Persist mic preference in localStorage
  const [micEnabled, setMicEnabled] = useState(() => {
    if (typeof localStorage !== 'undefined') {
      return localStorage.getItem('viola_spoke_mic') === 'true';
    }
    return false;
  });

  // Auto-start streaming when connected and mic is enabled
  useEffect(() => {
    if (connected && micEnabled && voiceStreamSupported && !isStreaming) {
      startStreaming();
    }
    if (!micEnabled && isStreaming) {
      stopStreaming();
    }
  }, [connected, micEnabled, voiceStreamSupported, isStreaming, startStreaming, stopStreaming]);

  const handleMicToggle = useCallback(() => {
    const next = !micEnabled;
    setMicEnabled(next);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('viola_spoke_mic', String(next));
    }
  }, [micEnabled]);

  // ----- Pre-connect screen ----- //
  if (!connected) {
    return (
      <div style={connectStyles.container}>
        <div style={connectStyles.card}>
          <h1 style={connectStyles.title}>Viola Speaker</h1>
          <p style={connectStyles.subtitle}>Multi-room audio receiver</p>

          <div style={connectStyles.fieldGroup}>
            <label style={connectStyles.label}>Speaker Name</label>
            <input
              type="text"
              value={speakerName}
              onChange={handleNameChange}
              style={connectStyles.input}
              placeholder="e.g. Kitchen, Bedroom"
              maxLength={30}
              data-testid="speaker-name-input"
            />
          </div>

          <button
            onClick={handleConnect}
            disabled={connecting}
            style={{
              ...connectStyles.connectBtn,
              ...(connecting ? { opacity: 0.7, cursor: 'not-allowed' } : {}),
            }}
            data-testid="connect-btn"
          >
            {connecting ? 'Connecting...' : 'Connect as Speaker'}
          </button>

          <p style={connectStyles.hint}>
            Room: {room}
          </p>
        </div>
      </div>
    );
  }

  // ----- Connected: SmartDisplay with overlays ----- //
  return (
    <div style={{
      width: '100vw',
      height: '100dvh',
      minHeight: '100dvh',
      overflowX: 'hidden',
      overflowY: 'auto',
      position: 'relative',
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'stretch',
    }}>
      <AuthProvider>
        <SmartDisplay isSpoke micStream={micStream} room={room} />
      </AuthProvider>

      {/* Overlays live outside SmartDisplay so position:fixed stays viewport-relative. */}

      {/* Fullscreen button — only shown when browser supports it */}
      {fullscreenSupported && (
        <FullscreenButton isFullscreen={isFullscreen} onToggle={toggleFullscreen} />
      )}

      {/* Android/Chrome install button — overlay top-left */}
      {installPrompt && (
        <button
          onClick={handleInstall}
          style={{
            position: 'fixed',
            top: '12px',
            left: '12px',
            zIndex: 9999,
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            padding: '6px 14px',
            borderRadius: '8px',
            border: `1px solid ${THEME.colors.accent}`,
            backgroundColor: 'rgba(0,0,0,0.4)',
            color: THEME.colors.accent,
            fontSize: '12px',
            fontWeight: 500,
            cursor: 'pointer',
            opacity: 0.7,
            transition: 'opacity 0.2s ease',
          }}
          onMouseOver={e => { e.currentTarget.style.opacity = '1'; }}
          onMouseOut={e => { e.currentTarget.style.opacity = '0.7'; }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Install
        </button>
      )}

      {/* PWA install hint (shown when not installed and no native install prompt) */}
      {showIOSInstall && (
        <IOSInstallBanner onDismiss={() => setShowIOSInstall(false)} />
      )}

      {/* Mic toggle for wake word streaming — bottom-left */}
      <MicToggle
        isStreaming={isStreaming}
        isSupported={voiceStreamSupported}
        onToggle={handleMicToggle}
      />

      {/* Status label under mic button */}
      {voiceStreamSupported && (
        <div style={{
          position: 'fixed',
          bottom: '4px',
          left: '16px',
          zIndex: 9999,
          width: '44px',
          textAlign: 'center',
          fontSize: '9px',
          color: isStreaming ? THEME.colors.accent : 'rgba(255,255,255,0.3)',
          pointerEvents: 'none',
        }}>
          {isStreaming ? 'Wake ON' : 'Wake OFF'}
        </div>
      )}

      {/* Wake detection banner */}
      <WakeBanner text={wakeBanner.text} visible={wakeBanner.visible} />

      {/* Background tab warning toast */}
      <BackgroundToast
        visible={showBackgroundToast}
        onDismiss={() => setShowBackgroundToast(false)}
      />
    </div>
  );
}

SpokeWrapper.propTypes = {
  room: PropTypes.string,
};

SpokeWrapper.defaultProps = {
  room: 'speaker',
};

// ----- Connect screen styles (matches SpokeAudioPage) ----- //
const connectStyles = {
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
  },
  title: {
    fontSize: 24,
    fontWeight: 600,
    margin: '0 0 4px 0',
    color: THEME.colors.textPrimary,
  },
  subtitle: {
    fontSize: 14,
    color: THEME.colors.textSecondary,
    margin: '0 0 24px 0',
  },
  fieldGroup: {
    marginBottom: 20,
  },
  label: {
    display: 'block',
    fontSize: 12,
    fontWeight: 600,
    color: THEME.colors.textTertiary,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
    marginBottom: 6,
  },
  input: {
    width: '100%',
    padding: '10px 12px',
    fontSize: 14,
    border: `1px solid ${THEME.colors.borderSubtle}`,
    borderRadius: 8,
    backgroundColor: THEME.colors.bgElevated,
    color: THEME.colors.textPrimary,
    outline: 'none',
    boxSizing: 'border-box',
  },
  connectBtn: {
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
  hint: {
    fontSize: 12,
    color: THEME.colors.textMuted,
    textAlign: 'center',
    marginTop: 16,
    marginBottom: 0,
  },
};
