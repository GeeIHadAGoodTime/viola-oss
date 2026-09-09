/* eslint react/jsx-uses-vars: "error" */
import React, { Suspense, lazy, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import PropTypes from 'prop-types';
import { usePlayerState } from './hooks/usePlayerState';
import { useWebSocket } from './hooks/useWebSocket';
import { useViolaApi, authFetch } from './hooks/useViolaApi';
import { useAuth } from './hooks/useAuth';
import { useAgentRegistry } from './hooks/useAgentRegistry';
import { useVoice } from './hooks/useVoice';
import { useVoiceWs } from './hooks/useVoiceWs';
import { describeError } from './utils/describeError';
import { resolveYouTubeEmbedUrl, youtubeEmbedFrameUrl, isYouTubeEmbedMessage } from './utils/youtubeEmbed';
import { useHandsFreeWake } from './hooks/useHandsFreeWake';
import { useBrowserWakeWord } from './hooks/useBrowserWakeWord';
import HandsFreeMicIndicator from './components/voice/HandsFreeMicIndicator';
import useCallAudio, { fetchActiveCall, fetchCallQueue, removeQueuedCall } from './hooks/useCallAudio';
import useCloudPhoneEvents from './hooks/useCloudPhoneEvents';
import ToastContainer, { useToast } from './components/Toast';
import TimerCountdown from './components/TimerCountdown';
import { useSettings } from './hooks/useSettings';
import { useAvailableRooms } from './hooks/useAvailableRooms';
import CallBriefing from './components/CallBriefing';
import CallConsultation from './components/CallConsultation';
import PhoneCallPanel from './components/PhoneCallPanel';
import LoginPromptModal from './components/LoginPromptModal';
import PhoneToSModal from './components/PhoneToSModal';
import CallHistoryList from './components/CallHistoryList';
import CallSummaryCard from './components/CallSummaryCard';
import BugReportModal from './components/BugReportModal';
import { THEME, applyTheme, setAccent } from './config';
import ErrorBoundary from './components/ErrorBoundary';
import Modal from './components/Modal';
import CalendarView from './components/CalendarView';
import WeatherForecast from './components/WeatherForecast';
import { useVoiceOnboarding } from './hooks/useVoiceOnboarding';
import OnboardingHighlight from './components/OnboardingHighlight';
import OnboardingOverlay from './components/OnboardingOverlay';
import { openSentryUserFeedback, syncSentrySettings } from './sentryClient';
import { formatTimeDisplay } from './utils/timeFormat';
import { UNKNOWN_CONDITION, describeCondition, normalizeConditionKey } from './utils/weatherCondition';
import { showNotification } from './utils/notifications';
import { prewarmTtsContext } from './utils/ttsPlayback';
import {
  DEFAULT_MUTE_HOTKEY,
  DEFAULT_PTT_HOTKEY,
  isCommandPaletteShortcut,
  isHotkeyEvent,
  shouldIgnoreCommandPaletteShortcut,
} from './utils/hotkeys';
import ContentCard from './components/ContentCard';
import WorkbenchDropZone from './components/WorkbenchDropZone';
import MemoryPanel from './components/MemoryPanel';
import { dispatchUiActions } from './utils/uiActions';
import { extractCapDenial } from './utils/capDenial';
import { isWebClient, shouldStreamBrowserView } from './utils/runtimeSurface';
import { isCloudSurface } from './components/auth/cloudSurface';
import CloudLlmConsentModal from './components/CloudLlmConsentModal';
import { useCloudLlmConsent } from './hooks/useCloudLlmConsent';
import { CloudConsentGateProvider, shouldPromptForCloudConsent } from './hooks/cloudConsentGate';
import CloudWelcome from './components/CloudWelcome';
import { useCloudWelcome } from './hooks/useCloudWelcome';
import { useAgentBrowserStream } from './hooks/useAgentBrowserStream';
import {
  setCommandPaletteOpen as setCommandPaletteOpenUiState,
  useUiSelector,
  useUiStateDispatch,
} from './state/uiState';

// Extracted components
import TopBar from './components/topbar/TopBar';
import PlayerSection from './components/player/PlayerSection';
import Stage from './components/stage/Stage';
import PillBar from './components/stage/PillBar';
import CommandPalette from './components/stage/CommandPalette';
import useCommandRegistry from './components/stage/useCommandRegistry';
import {
  createBrowserStageCommands,
  createChatStageCommands,
  createCoreStageCommands,
} from './components/stage/stageCommandContributions';
import BrowserMode from './components/stage/modes/browser/BrowserMode';
import ChatMode from './components/stage/modes/chat';
import BottomRow from './components/voice/BottomRow';
import DesktopRelayIndicator, { relayDeviceFromResult } from './components/DesktopRelayIndicator';
import SubtleDivider from './components/shared/SubtleDivider';
import LoadingSkeleton from './components/shared/LoadingSkeleton';
import ModalLoadingSpinner from './components/shared/ModalLoadingSpinner';
import ProviderBadge from './components/shared/ProviderBadge';
import AgentDrawer from './components/AgentDrawer';

// Shared CSS: animations + variables (replaces all inline <style> tags)
import './styles/variables.css';
import './styles/animations.css';
import './styles/responsive-smart-display.css';

// Code-split modals - only loaded when opened (Sprint 5 optimization)
const SettingsModal = lazy(() => import('./components/SettingsModal'));
const QueueModal = lazy(() => import('./components/QueueModal'));
const HistoryModal = lazy(() => import('./components/HistoryModal'));
const RoomGroupsModal = lazy(() => import('./components/RoomGroupsModal'));
const HelpModal = lazy(() => import('./components/HelpModal'));

/**
 * ChunkLoadErrorBoundary — catches dynamic-import 404s that occur when the Qt
 * webview serves a stale cached index.js whose lazy-chunk map points at files
 * deleted by a Vite rebuild.
 *
 * Recovery strategy: perform ONE reload (index.html is served no-cache so the
 * fresh chunk map is guaranteed). A sessionStorage flag prevents infinite loops
 * if the chunk is genuinely missing after the reload.
 *
 * The reload-guard flag is keyed PER boundary `name` (e.g. "Settings",
 * "Queue"), not a single shared key. #1422 root cause: five independent lazy
 * modals (Settings/Queue/History/Rooms/Help) each mount their own
 * ChunkLoadErrorBoundary, but a single global `chunk_reload_attempted` key
 * meant ANY one of them hitting a transient chunk-load hiccup burned the
 * one-shot reload budget for all the others for the rest of the browser
 * session — a later, otherwise-healthy Settings chunk load would skip
 * straight to the "couldn't load" fallback because Queue (or any other
 * modal) had already spent the shared flag. Scoping the key per-name gives
 * each modal its own independent one-shot budget.
 */
const CHUNK_RELOAD_KEY_PREFIX = 'chunk_reload_attempted';

function chunkReloadKey(name) {
  return `${CHUNK_RELOAD_KEY_PREFIX}:${name || 'unknown'}`;
}

function isChunkLoadError(error) {
  if (!error) return false;
  const msg = (error.message || '') + (error.name || '');
  return (
    error.name === 'ChunkLoadError' ||
    /Failed to fetch dynamically imported module/i.test(msg) ||
    /Loading chunk \S+ failed/i.test(msg) ||
    /importing a module script failed/i.test(msg)
  );
}

class ChunkLoadErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError(error, props) {
    if (isChunkLoadError(error)) {
      const reloadKey = chunkReloadKey(props?.name);
      // One-shot reload guard, scoped per boundary name: only reload if THIS
      // modal's chunk hasn't already tried a reload this session.
      if (!sessionStorage.getItem(reloadKey)) {
        sessionStorage.setItem(reloadKey, '1');
        window.location.reload();
        // Return null — keep rendering the loading spinner while the page reloads.
        return null;
      }
      // Already reloaded once for this modal; show fallback UI instead of looping.
      return { failed: true };
    }
    // Non-chunk errors bubble up to the root Sentry boundary.
    throw error;
  }

  componentDidCatch(error, info) {
    if (isChunkLoadError(error)) {
      console.error('[ChunkLoadErrorBoundary] chunk load failed after reload guard:', error, info);
    }
  }

  render() {
    if (this.state.failed) {
      // Discoverable as a modal-in-terminal-failure state (role="dialog" +
      // aria-modal + data-testid), not a bare unlabeled <div> — a DOM/a11y
      // scan for "did a modal mount" must be able to see this state instead
      // of reading it as "nothing happened" (#1422).
      return (
        <div
          role="dialog"
          aria-modal="true"
          data-testid={`chunk-load-error-${(this.props.name || 'panel').toLowerCase()}`}
          style={{ padding: '16px', color: 'rgba(255,255,255,0.5)', fontSize: '13px' }}
        >
          {this.props.name || 'Panel'} couldn&apos;t load — please restart Viola.
        </div>
      );
    }
    return this.props.children;
  }
}

ChunkLoadErrorBoundary.propTypes = {
  children: PropTypes.node.isRequired,
  name: PropTypes.string,
};

// The response line under the media tile is a single clipped strip
// (BottomRow.module.css `.responseArea` is `overflow: hidden`), so a long
// answer that also went to a card gets cut mid-word with no sign it was cut.
// Trim it to a readable lead with a visible ellipsis instead.
const RESPONSE_LINE_MAX_CHARS = 160;

export function truncateForResponseLine(text) {
  const value = typeof text === 'string' ? text.trim() : '';
  if (value.length <= RESPONSE_LINE_MAX_CHARS) return value;
  // Prefer cutting at a word boundary so the lead reads as a sentence.
  const head = value.slice(0, RESPONSE_LINE_MAX_CHARS);
  const lastSpace = head.lastIndexOf(' ');
  const cut = lastSpace > RESPONSE_LINE_MAX_CHARS * 0.6 ? head.slice(0, lastSpace) : head;
  return `${cut.replace(/[\s.,;:!?-]+$/, '')}...`;
}

const WEATHER_FORECAST_TTL_MS = 30 * 60 * 1000;

function hasForecastPayload(data) {
  const hourly = data?.hourly_forecast || data?.hourly;
  const daily = data?.daily_forecast || data?.daily || data?.forecast;
  return Array.isArray(hourly) && hourly.length > 0 && Array.isArray(daily) && daily.length > 0;
}

const SETTINGS_TAB_ALIASES = {
  ai: 'ai_agents',
  ai_agents: 'ai_agents',
  agents: 'ai_agents',
  agent: 'ai_agents',
  account: 'account',
  accounts: 'account',
  profile: 'account',
  music: 'music',
  music_voice: 'music',
  voice: 'voice',
  music_accounts: 'music',
  services: 'connections',
  connected_services: 'connections',
  connections: 'connections',
  messaging: 'connections',
  messages: 'connections',
  preferences: 'customize',
  appearance: 'customize',
  weather: 'customize',
  customize: 'customize',
  payment: 'account',
  payments: 'account',
  payment_methods: 'account',
  system: 'system',
  developer: 'system',
};

export const AGENT_CONTEXT_PILL_COOLDOWN_MS = 12000;

function getStageContentMode(displayMode) {
  if (displayMode === 'phone_tab' || displayMode === 'phone_call') return 'phone';
  if (displayMode === 'chat') return 'chat';
  if (displayMode === 'browser' || displayMode === 'agentic_task') return 'browser';
  return 'music';
}

function getActiveStagePillMode(displayMode) {
  if (displayMode === 'phone_tab' || displayMode === 'phone_call') return 'phone';
  if (displayMode === 'chat') return 'chat';
  if (displayMode === 'browser') return 'browser';
  if (displayMode === 'agentic_task') return 'agent';
  return 'music';
}

// Decides which stage CONTENT actually renders, given the current display mode plus
// live signals. Two rules the raw displayMode map can't express:
//   1. A live phone call does NOT auto-steal the stage (founder direction
//      2026-06-29: placing a call must NOT auto-open the phone tab). But while
//      the user IS on the phone tab during a call, the phone stage is HELD — a
//      concurrent agentic-task/browser signal can't flip the visible stage to
//      the browser webview mid-call out from under the live transcript/takeover.
//      Reflecting the live call when the user opens the phone tab is handled by
//      activeCallId-driven recovery + the phone stage rendering, not by forcing
//      the stage here.
//   2. The browser webview is for GENUINE web browsing only — never an empty about:blank
//      panel for non-browsing agent tasks (phone, memory, etc.). An agentic task that is
//      not actually browsing (no bridge activation, no live frames, no real page URL)
//      falls back to the music/now_playing stage; the agent pill still signals activity.
export function resolveStageContentMode(displayMode, signals = {}) {
  const { activeCallId, browserModeActive, agentFrameSrc, browserUrl } = signals;
  const base = getStageContentMode(displayMode);
  // Hold the phone stage only when the user is already viewing it during a live
  // call — never force a switch to it.
  if (activeCallId && base === 'phone') return 'phone';
  if (base === 'browser' && displayMode === 'agentic_task') {
    const reallyBrowsing = Boolean(browserModeActive)
      || Boolean(agentFrameSrc)
      || (Boolean(browserUrl) && browserUrl !== 'about:blank');
    if (!reallyBrowsing) return 'music';
  }
  return base;
}

// The pill label must tell the truth about turn state (#1407). A present-tense
// verb ("Working...", "Searching...") is a claim of *active* work, so it may
// only appear while the agent is genuinely active. Once the turn completes,
// `active` is false and the pill lingers briefly (its 12s cooldown affordance,
// per the stage-pill design) as an icon-only, resolved indicator — never
// "Working...". Without this gate, every completed turn left a stale "Working..."
// pill spinning through the whole cooldown, which the UX audit captured across
// idle turns as a spinner that never resolves.
// The first-run consent rule lives in hooks/cloudConsentGate.jsx and is
// re-exported here because it is imported from this module by name (#362).
//
// Sharing it as a plain function was not enough: only call sites that also hold
// this component's consent/modal state could use it, so every turn entry point
// living in its OWN component stayed ungated -- which is how the chat-mode
// composer shipped with no gate right after the text composer got one. The
// context in that module is what actually reaches them (#4667 follow-up).
export { shouldPromptForCloudConsent };

export function getAgentPillLabel(status, phase, active = true) {
  if (!active) return '';
  const cleanStatus = (status || '').trim();
  if (cleanStatus) {
    const firstWords = cleanStatus.split(/\s+/).slice(0, 3).join(' ');
    return firstWords.length > 26 ? `${firstWords.slice(0, 23)}...` : firstWords;
  }
  if (phase === 'thinking') return 'Thinking...';
  if (phase === 'reading') return 'Reading page...';
  if (phase === 'searching') return 'Searching...';
  return 'Working...';
}

// What to tell the user when a transport control's API call rejects.
//
// `apiFetch` (hooks/useViolaApi.js) deliberately throws ONE generic message —
// "We couldn't complete that request. Please try again." — for every non-2xx
// response, so route paths and server internals can never reach the screen. It
// keeps the machine-readable `status`/`code` on the error instead. So the
// STATUS decides the copy here; `err.message` is the same generic sentence for
// an end-of-queue click and for a dead player alike and must never be shown.
//
// A rejection carrying no numeric `status` never reached the server at all
// (offline, DNS, aborted — `fetch` rejects with a bare TypeError). The dropped
// connection is already announced once, by the WebSocket disconnect toast and
// the bottom-row "Disconnected" label, so a per-click toast would only stack
// duplicates on top of it: this returns null, meaning "the rejection is
// handled, say nothing further".
//
// Returns `{ message, level }`, or `null` for "handled, stay quiet".
export function describeTransportFailure(err, { refused, failed }) {
  if (typeof err?.status !== 'number') return null;
  // 409 Conflict is what the control routes answer when the player refuses an
  // ordinary state — `empty_queue`, `operation_not_allowed` (see
  // ui/api/routes/control.py). That is the end of the queue, not a fault, so
  // it reads as information rather than an error.
  if (err.status === 409) return { message: refused, level: 'info' };
  return { message: failed, level: 'error' };
}

// Player progress must tell the truth about playback state (#1407). With no
// track loaded the transport reads "Nothing playing", so position/duration/
// progress are forced to 0 regardless of any stale position/duration the
// backend leaves in its last state broadcast after a stop. With a track, YouTube
// hub playback reads the live iframe clock; everything else reads the backend
// position/duration.
export function computePlayerDisplayMetrics({
  hasTrack,
  isYouTubeVideo,
  isSpoke,
  iframePosition = 0,
  iframeDuration = 0,
  position = 0,
  duration = 0,
}) {
  if (!hasTrack) {
    return { displayPosition: 0, displayDuration: 0, displayProgress: 0 };
  }
  const useIframeClock = isYouTubeVideo && !isSpoke;
  const displayPosition = useIframeClock ? iframePosition : (position || 0);
  const displayDuration = useIframeClock ? iframeDuration : (duration || 0);
  const displayProgress = displayDuration > 0 ? (displayPosition / displayDuration) * 100 : 0;
  return { displayPosition, displayDuration, displayProgress };
}

function formatCallTitleElapsed(startedAt, fallbackMs = Date.now()) {
  const rawParsed = typeof startedAt === 'string' && startedAt
    ? Date.parse(startedAt)
    : Number(startedAt);
  const parsed = Number.isFinite(rawParsed) && rawParsed > 0 && rawParsed < 1000000000000
    ? rawParsed * 1000
    : rawParsed;
  const startedMs = Number.isFinite(parsed) ? parsed : fallbackMs;
  const totalSeconds = Math.max(0, Math.floor((Date.now() - startedMs) / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
  const seconds = String(totalSeconds % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}

// =========================================================================
// PURE COMPONENTS - Defined at module scope to prevent unmount/remount on
// every parent re-render.  When defined inside SmartDisplay, React sees a
// new component type each render cycle, destroying and recreating the DOM
// element — which resets CSS :hover state and detaches click handlers,
// causing the "flicker" bug.
// =========================================================================

// Embedded YouTube Player component - MUST be defined outside SmartDisplay
// to prevent React from recreating it on every parent render
const SPOKE_EMBED_URL = resolveYouTubeEmbedUrl(import.meta.env.VITE_YOUTUBE_EMBED_URL);
const SPOKE_EMBED_ORIGIN = new URL(SPOKE_EMBED_URL).origin;

const YouTubeEmbed = ({ videoId, iframeRef, isSpoke = false }) => {
  // Keep the spoke's initial video stable so the cross-origin embed iframe is
  // created once; later track changes are driven via postMessage (viola_play),
  // not by recreating the iframe.
  const spokeInitialVideoRef = useRef(videoId);

  if (!videoId) return null;

  // A multiroom spoke is a real browser, usually at an http://<lan-ip> origin.
  // YouTube refuses to embed major-label videos from such origins (verified
  // live: Error 150 / errorCode "auth"). So a spoke embeds through
  // a separately hosted HTTPS helper, which hosts the YouTube IFrame player
  // and relays state/commands over postMessage. Its host is configurable.
  // The hub keeps its OWN local iframe because the Qt webview forges an
  // authorized Referer (localhost) that a plain browser cannot. Do NOT collapse
  // these two paths (regressed 905f1d7a / codex 2026-06). See
  // docs/EMBEDDED_PLAYBACK_ARCHITECTURE.md.
  if (isSpoke) {
    return (
      <iframe
        ref={iframeRef}
        src={youtubeEmbedFrameUrl(SPOKE_EMBED_URL, spokeInitialVideoRef.current, window.location.origin)}
        style={{ width: '100%', height: '100%', border: 'none', borderRadius: '16px' }}
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowFullScreen
        playsInline
        title="YouTube video player"
        tabIndex={-1}
        referrerPolicy="strict-origin-when-cross-origin"
      />
    );
  }

  const FILE_VERSION = 9;
  const cacheBust = (videoId.split('').reduce((acc, c) => ((acc << 5) - acc) + c.charCodeAt(0), 0) >>> 0) + FILE_VERSION;
  // The desktop hub runs inside Qt WebEngine, which honours a programmatic
  // unmuted playVideo(); a plain browser does NOT. On the cloud SPA an
  // `autoplay=1&mute=0` embed is blocked every time: the player reports
  // UNSTARTED, burns three playVideo() retries, falls into a muted-start
  // workaround and settles PAUSED a fraction of a second in with nothing
  // audible (measured on the deployed webview, #3552). So the cloud surface
  // CUES the video and lets the user's click on YouTube's own controls be the
  // gesture that starts it -- the same `requires_user_gesture` the cloud
  // playback plan already declares (services/cloud_music/playback_plan.py).
  const wantsAutoplay = isCloudSurface() ? 0 : 1;
  const embedUrl = `/static/webviews/youtube_iframe_v3.html?video=${videoId}&autoplay=${wantsAutoplay}&mute=0&_v=${FILE_VERSION}&_cb=${cacheBust}`;

  return (
    <iframe
      ref={iframeRef}
      key={videoId}
      src={embedUrl}
      style={{ width: '100%', height: '100%', border: 'none', borderRadius: '16px' }}
      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
      allowFullScreen
      title="YouTube video player"
      tabIndex={-1}
      referrerPolicy="strict-origin-when-cross-origin"
    />
  );
};

YouTubeEmbed.propTypes = {
  videoId: PropTypes.string,
  iframeRef: PropTypes.object,
  isSpoke: PropTypes.bool,
};

// Provider Embed component - module scope to prevent remount
const ProviderEmbed = ({ embedUrl, embedType, providerName }) => {
  if (!embedUrl) return null;

  return (
    <iframe
      key={embedUrl}
      src={embedUrl}
      style={{ width: '100%', height: '100%', border: 'none', borderRadius: '16px', opacity: 1, transition: 'opacity 0.3s ease' }}
      allow="autoplay; clipboard-write; encrypted-media"
      loading="lazy"
      title={`${providerName || 'Music'} player`}
      referrerPolicy="strict-origin-when-cross-origin"
      sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
    />
  );
};

ProviderEmbed.propTypes = {
  embedUrl: PropTypes.string,
  embedType: PropTypes.string,
  providerName: PropTypes.string,
};

// =========================================================================
// END OF MODULE-SCOPE COMPONENTS
// =========================================================================

export default function SmartDisplay({ isSpoke = false, micStream = null, room = null }) {

  // Real state from backend (includes send for WebSocket commands, setLocalIsPlaying for optimistic updates)
  const playerState = usePlayerState();
  const [spokeBackendTimedOut, setSpokeBackendTimedOut] = useState(false);
  const { send: wsSend, setLocalIsPlaying, setDiagnosticRequestCallback, setErrorCallback, setOverlayCallback, setAgentProgressCallback, setDisplayPriorityCallback, setAgentFrameCallback, setPlaybackCommandCallback, setChatResponseCallback, setCalendarUpdateCallback, setDisconnectCallback } = playerState;
  const api = useViolaApi();
  const {
    settings: userSettings,
    loading: settingsLoading,
    refreshSettings,
    voiceStatus,
    updateSetting,
  } = useSettings();

  // Authoritative account identity (GoTrue session) — the SAME source the
  // Account tab reads. The sidebar identity chip must agree with it, so we
  // derive the profile name from the signed-in account first and only fall
  // back to the local device display name when signed out (issue #772).
  const { user: accountUser } = useAuth();

  useEffect(() => {
    syncSentrySettings(userSettings);
  }, [userSettings]);

  // Theme: force re-render when theme colors change (THEME is mutated in place)
  const [, setThemeVersion] = useState(0);

  // Apply theme whenever the user setting changes (or on mount)
  useEffect(() => {
    const mode = userSettings?.theme || 'dark';
    if (mode === 'system') {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const apply = () => {
        applyTheme('system');
        setThemeVersion(v => v + 1);
      };
      apply();
      mq.addEventListener('change', apply);
      return () => mq.removeEventListener('change', apply);
    } else {
      applyTheme(mode);
      setThemeVersion(v => v + 1);
    }
  }, [userSettings?.theme]);

  // Apply saved accent color whenever the user setting changes (or on mount).
  // Falls back to the canonical default baked into variables.css when unset.
  useEffect(() => {
    const accent = userSettings?.accent_color;
    if (accent && typeof accent === 'string') {
      setAccent(accent);
    }
  }, [userSettings?.accent_color]);

  // UI state
  const [currentTime, setCurrentTime] = useState(new Date());
  const [menuOpen, setMenuOpen] = useState(false);
  // Starts unknown on purpose: until a payload arrives we have nothing to
  // claim about the sky, and the previous partly-cloudy seed made a topbar that
  // never loaded look like a live, sunny forecast.
  const [weatherCondition, setWeatherCondition] = useState(UNKNOWN_CONDITION);
  const [weatherTemp, setWeatherTemp] = useState('--');
  const [weatherDesc, setWeatherDesc] = useState('Loading...');
  const [weatherData, setWeatherData] = useState(null);
  const [weatherRetryTrigger, setWeatherRetryTrigger] = useState(0);
  const [weatherForecastOpen, setWeatherForecastOpen] = useState(false);
  const [weatherForecastAnchor, setWeatherForecastAnchor] = useState(null);
  const [weatherForecastData, setWeatherForecastData] = useState(null);
  const [weatherForecastLoading, setWeatherForecastLoading] = useState(false);
  const [weatherForecastError, setWeatherForecastError] = useState('');
  const [weatherForecastFetchedAt, setWeatherForecastFetchedAt] = useState(0);
  const [shuffleOn, setShuffleOn] = useState(playerState.shuffle || false);
  const [repeatMode, setRepeatMode] = useState(playerState.repeat_mode || 'off');

  // =========================================================================
  // DISPLAY MODE STATE - now_playing, phone_tab, browser, agentic_task,
  // calendar
  // =========================================================================
  const [displayMode, setDisplayMode] = useState('now_playing');
  const [browserModeActive, setBrowserModeActive] = useState(false);
  const [browserUrl, setBrowserUrl] = useState('');
  const [calendarModeActive, setCalendarModeActive] = useState(false);
  const [agentTaskActive, setAgentTaskActive] = useState(false);
  const [agentTaskDescription, setAgentTaskDescription] = useState('');
  const [agentTaskStatus, setAgentTaskStatus] = useState('');
  const [agentTaskPhase, setAgentTaskPhase] = useState('');
  const [agentTakeoverActive, setAgentTakeoverActive] = useState(false);
  const [agentContextPillVisible, setAgentContextPillVisible] = useState(false);
  const [displayPriorityOverride, setDisplayPriorityOverride] = useState(null);
  const commandPaletteOpen = useUiSelector((state) => state.commandPaletteOpen);
  const uiStateDispatch = useUiStateDispatch();
  const setCommandPaletteOpen = useCallback((open) => {
    uiStateDispatch(setCommandPaletteOpenUiState(open));
  }, [uiStateDispatch]);

  // Agent frame streaming state (spoke + cloud/LAN web client)
  const [agentFrameSrc, setAgentFrameSrc] = useState(null);
  const [agentFrameFade, setAgentFrameFade] = useState(null);
  const agentFrameUrlRef = useRef(null);

  // Whether the Stage Browser tab should render the agent's browser as
  // streamed JPEG frames instead of positioning a native embedded webview.
  // True for multiroom spokes AND for cloud/LAN web clients (a plain
  // browser has no Qt bridge, so a native webview is impossible). Only the
  // desktop app keeps the native-webview path.
  const streamBrowserView = useMemo(() => shouldStreamBrowserView(isSpoke), [isSpoke]);

  // Dedicated cloud agent-browser stream (`/ws/agent-browser`). Only a
  // cloud/LAN web client (no Qt bridge, not a multiroom spoke) opens it —
  // spokes keep relaying agent frames over the shared `/ws/events` socket.
  // Enable it when the browser stage is active or an agentic browser task is
  // running, so public web-browse renders in the stage area.
  const useDedicatedBrowserStream = useMemo(
    () => isWebClient() && !isSpoke,
    [isSpoke],
  );
  const agentBrowserStreamEnabled = useDedicatedBrowserStream
    && (displayMode === 'browser' || displayMode === 'agentic_task' || agentTaskActive);
  const {
    frameSrc: dedicatedFrameSrc,
    frameFade: dedicatedFrameFade,
    status: agentBrowserStreamStatus,
    streamError: agentBrowserStreamError,
    sendInput: agentBrowserSendInput,
  } = useAgentBrowserStream({ enabled: agentBrowserStreamEnabled });

  // Browser overlay refs
  const stageRef = useRef(null);
  const mediaAreaRef = useRef(null);
  const [browserOverlayVisible, setBrowserOverlayVisible] = useState(false);

  // Modal state
  // TopBar's expanded-calendar modal (CalendarView.jsx, compact mode) is
  // owned by CalendarView's own local state, not lifted here by default --
  // this mirror lets it participate in anyModalOpen below (#2568: the
  // calendar modal's backdrop wasn't hiding viola-inner-card behind it,
  // so background text visually overlapped the modal on every viewport).
  const [calendarModalOpen, setCalendarModalOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialTab, setSettingsInitialTab] = useState(null);
  const [settingsInitialSection, setSettingsInitialSection] = useState(null);
  const [queueOpen, setQueueOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [roomsOpen, setRoomsOpen] = useState(false);
  const [roomsInitialTab, setRoomsInitialTab] = useState('add-speaker');
  const [roomsPrefill, setRoomsPrefill] = useState(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [bugReportOpen, setBugReportOpen] = useState(false);
  const [workbenchPanelOpen, setWorkbenchPanelOpen] = useState(false);
  const [loginPromptPayload, setLoginPromptPayload] = useState(null);
  const [phoneTosPayload, setPhoneTosPayload] = useState(null);

  // Phone-call UX toasts, driven by backend WebSocket events from
  // telephony/call_tools.py (`call_briefing` when a plan is proposed, and
  // `call_consultation` when mid-call guidance is asked).  Each toast
  // clears either on dismiss or when a new event of the same type arrives.
  const [callBriefing, setCallBriefing] = useState(null);
  const [callConsultation, setCallConsultation] = useState(null);
  const [lastEndedCall, setLastEndedCall] = useState(null);
  const [phoneHistoryFocusCallId, setPhoneHistoryFocusCallId] = useState(null);
  // Track the current active phone call and the metadata needed for the
  // in-call display panel.
  const [activeCallId, setActiveCallId] = useState(null);
  const [activeCallMeta, setActiveCallMeta] = useState(null);
  const [phoneCallQueue, setPhoneCallQueue] = useState([]);
  const [callOwnerTakeoverPending, setCallOwnerTakeoverPending] = useState(false);
  const {
    isListening: callAudioListening,
    startListening: startCallListening,
    stopListening: stopCallListening,
    takeoverActive: callTakeoverActive,
    startTakeover: startCallTakeover,
    releaseTakeover: releaseCallTakeover,
    requestOwnerTakeover,
    transcripts: callTranscripts,
    latestCostUsd: callAudioLatestCostUsd,
    recipientState: callAudioRecipientState,
    sendOperatorMessage: sendCallOperatorMessage,
    endCall,
    pushTranscript: pushCallTranscript,
    applyCostUpdate: applyCallCostUpdate,
  } = useCallAudio(activeCallId);
  const callFallbackStartRef = useRef(Date.now());
  const pendingCallTakeoverRef = useRef(false);
  const activeCallPanelMeta = useMemo(() => {
    const merged = {
      ...(callBriefing || {}),
      ...(activeCallMeta || {}),
      call_id: activeCallId,
    };
    if (activeCallId && !merged.started_at) {
      merged.started_at = new Date(callFallbackStartRef.current).toISOString();
    }
    if (callAudioLatestCostUsd !== null && callAudioLatestCostUsd !== undefined) {
      merged.current_cost_usd = callAudioLatestCostUsd;
    }
    if (callAudioRecipientState) {
      merged.recipient_state = callAudioRecipientState;
    }
    return merged;
  }, [activeCallMeta, callBriefing, activeCallId, callAudioLatestCostUsd, callAudioRecipientState]);
  const isPhoneMode = displayMode === 'phone_tab' || displayMode === 'phone_call';
  const refreshPhoneCallQueue = useCallback(async () => {
    try {
      setPhoneCallQueue(await fetchCallQueue());
    } catch (_) {
      setPhoneCallQueue([]);
    }
  }, []);
  const handleRemoveQueuedCall = useCallback(async (position) => {
    try {
      const result = await removeQueuedCall(position);
      if (Array.isArray(result?.queue)) {
        setPhoneCallQueue(result.queue);
      } else {
        await refreshPhoneCallQueue();
      }
    } catch (_) {
      await refreshPhoneCallQueue();
    }
  }, [refreshPhoneCallQueue]);
  useEffect(() => {
    if (isPhoneMode || activeCallId) {
      void refreshPhoneCallQueue();
    }
  }, [activeCallId, isPhoneMode, refreshPhoneCallQueue]);
  const activeInlineConsultation = useMemo(() => {
    if (!isPhoneMode || !activeCallId || !callConsultation) return null;
    const consultationCallId = callConsultation.call_id || callConsultation.id;
    if (consultationCallId && consultationCallId !== activeCallId) return null;
    return callConsultation;
  }, [activeCallId, callConsultation, isPhoneMode]);
  const callTitleConsultationPending = useMemo(() => {
    if (!callConsultation) return false;
    const consultationCallId = callConsultation.call_id || callConsultation.id;
    return !activeCallId || !consultationCallId || consultationCallId === activeCallId;
  }, [activeCallId, callConsultation]);

  useEffect(() => {
    const setCallTitle = () => {
      if (callTitleConsultationPending) {
        document.title = '\u2753 Viola needs you - Viola';
        return;
      }
      if (activeCallId) {
        const recipient = activeCallPanelMeta.phone_number || activeCallPanelMeta.phone || activeCallPanelMeta.to || 'phone call';
        const elapsed = formatCallTitleElapsed(activeCallPanelMeta.started_at, callFallbackStartRef.current);
        document.title = `\u{1F4DE} Calling ${recipient} \u2014 ${elapsed} - Viola`;
        return;
      }
      document.title = 'Viola';
    };

    setCallTitle();
    if (!activeCallId) {
      return () => {
        document.title = 'Viola';
      };
    }
    const titleTimer = window.setInterval(setCallTitle, 10000);
    return () => {
      window.clearInterval(titleTimer);
      document.title = 'Viola';
    };
  }, [
    activeCallId,
    activeCallPanelMeta.phone,
    activeCallPanelMeta.phone_number,
    activeCallPanelMeta.started_at,
    activeCallPanelMeta.to,
    callTitleConsultationPending,
  ]);

  const handleToggleCallListen = useCallback(() => {
    if (callAudioListening) {
      stopCallListening();
    } else {
      startCallListening();
    }
  }, [callAudioListening, startCallListening, stopCallListening]);

  const handleToggleCallTakeover = useCallback(() => {
    if (callTakeoverActive) {
      releaseCallTakeover();
    } else {
      startCallTakeover();
    }
  }, [callTakeoverActive, releaseCallTakeover, startCallTakeover]);

  const handleActivateCallTakeover = useCallback(() => {
    if (callTakeoverActive || callOwnerTakeoverPending || !activeCallId) return;
    setCallOwnerTakeoverPending(true);
    void requestOwnerTakeover(activeCallId).finally(() => {
      setCallOwnerTakeoverPending(false);
    });
  }, [activeCallId, callOwnerTakeoverPending, callTakeoverActive, requestOwnerTakeover]);

  useEffect(() => {
    if (!pendingCallTakeoverRef.current || !callAudioListening || callTakeoverActive) return;
    pendingCallTakeoverRef.current = false;
    void startCallTakeover();
  }, [callAudioListening, callTakeoverActive, startCallTakeover]);

  useEffect(() => {
    pendingCallTakeoverRef.current = false;
  }, [activeCallId]);

  const handleEndActiveCall = useCallback(() => {
    if (activeCallId) {
      void endCall(activeCallId);
    }
  }, [activeCallId, endCall]);
  const checkPhoneTosStatus = useCallback(async () => {
    try {
      const response = await authFetch('/v1/phone/tos-status');
      if (!response.ok) return;
      const json = await response.json();
      const status = json?.data || json || {};
      if (status.accepted === false) {
        setPhoneTosPayload({
          error_code: 'phone_tos_required',
          message: 'Accept the Phone Calling Terms of Service before making calls.',
        });
      }
    } catch {
      // Best-effort prompt; tool-call errors still trigger the modal.
    }
  }, []);
  const handleTogglePhoneMode = useCallback(() => {
    setDisplayMode((mode) => {
      const nextMode = mode === 'phone_tab' || mode === 'phone_call' ? 'now_playing' : 'phone_tab';
      if (nextMode === 'phone_tab') {
        void checkPhoneTosStatus();
      }
      return nextMode;
    });
  }, [checkPhoneTosStatus]);
  const handleOpenPhoneHistory = useCallback(() => {
    setPhoneHistoryFocusCallId(null);
    setDisplayMode('phone_tab');
    void checkPhoneTosStatus();
  }, [checkPhoneTosStatus]);
  const handleViewEndedTranscript = useCallback(() => {
    const callId = lastEndedCall?.call_id;
    if (!callId) return;
    setPhoneHistoryFocusCallId(callId);
    setDisplayMode('phone_tab');
    setLastEndedCall(null);
  }, [lastEndedCall?.call_id]);
  const activateMusicMode = useCallback(() => {
    setBrowserModeActive(false);
    setCalendarModeActive(false);
    setDisplayPriorityOverride(agentTaskActive ? 'now_playing' : null);
    setDisplayMode('now_playing');
  }, [agentTaskActive]);
  const handleStageModeSelect = useCallback((nextMode) => {
    const activeStageMode = getActiveStagePillMode(displayMode);
    const targetMode = activeStageMode === nextMode || (nextMode === 'agent' && (activeStageMode === 'agent' || activeStageMode === 'browser'))
      ? 'music'
      : nextMode;

    if (targetMode === 'music') {
      activateMusicMode();
      return;
    }

    setDisplayPriorityOverride(null);
    if (targetMode === 'chat') {
      setBrowserModeActive(false);
      setCalendarModeActive(false);
      setDisplayMode('chat');
      return;
    }

    if (targetMode === 'phone') {
      setPhoneHistoryFocusCallId(null);
      setBrowserModeActive(false);
      setCalendarModeActive(false);
      handleTogglePhoneMode();
      return;
    }

    if (targetMode === 'agent') {
      setCalendarModeActive(false);
      if (browserModeActive || browserUrl) {
        setDisplayMode('browser');
      } else {
        setDisplayMode('agentic_task');
      }
    }
  }, [activateMusicMode, browserModeActive, browserUrl, displayMode, handleTogglePhoneMode]);
  const handleConsultationReply = useCallback(async (callId, answer) => {
    // Best-effort reply — the backend's primary input path is `ask_user`
    // (voice), so this only lands if a `/v1/phone/call/{id}/reply` endpoint
    // is wired.  Errors are swallowed so the toast still dismisses cleanly.
    try {
      await authFetch(`/v1/phone/call/${encodeURIComponent(callId)}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ answer }),
      });
    } catch {
      /* silent — see comment above */
    }
    setCallConsultation(null);
  }, []);
  const handleConsultationTakeover = useCallback(async (callId) => {
    if (!callId || callOwnerTakeoverPending) return;
    setCallOwnerTakeoverPending(true);
    try {
      await requestOwnerTakeover(callId, 'Recipient requested the owner join this call.');
      setCallConsultation(null);
    } finally {
      setCallOwnerTakeoverPending(false);
    }
  }, [callOwnerTakeoverPending, requestOwnerTakeover]);
  const handleSettingsClose = useCallback(() => {
    setSettingsOpen(false);
    setSettingsInitialTab(null);
    setSettingsInitialSection(null);
    refreshSettings();
  }, [refreshSettings]);

  // Multi-room
  const { rooms: availableRooms } = useAvailableRooms();

  useEffect(() => {
    if (!isSpoke || playerState.connected) {
      setSpokeBackendTimedOut(false);
      return undefined;
    }
    const timer = setTimeout(() => {
      setSpokeBackendTimedOut(true);
    }, 8000);
    return () => clearTimeout(timer);
  }, [isSpoke, playerState.connected]);

  // Toast notification state
  const { toasts, addToast, removeToast } = useToast();

  // Conversational onboarding
  const onboarding = useVoiceOnboarding();

  // Chat state
  const [chatHistory, setChatHistory] = useState([]);
  const [lastResponse, setLastResponse] = useState('"Hi! Ask me anything or tell me to play some music."');
  const [mainThinking, setMainThinking] = useState('');
  // Managed-AI usage cap (C-077). Non-null for as long as the plan allowance is
  // spent, so the response area can offer a route to the upgrade surface rather
  // than dead-ending on cap copy the user cannot act on. Cleared by the first
  // turn that comes back without a cap denial.
  const [capDenial, setCapDenial] = useState(null);
  // The user's desktop device name when the last turn ran on their desktop
  // via the cloud->desktop auto-link; null when it ran in the cloud.
  const [relayDeviceName, setRelayDeviceName] = useState(null);
  const [isCommandLoading, setIsCommandLoading] = useState(false);
  const streamingResponseRef = useRef('');
  const [activeCard, setActiveCard] = useState(null);
  const dismissCard = useCallback(() => setActiveCard(null), []);
  const [agentDrawerExpanded, setAgentDrawerExpanded] = useState(true);
  const handleAgentResult = useCallback(({ content }) => {
    setLastResponse(content);
    setMainThinking('');
    setChatHistory(prev => [
      ...prev,
      { role: 'assistant', content, timestamp: new Date().toISOString() },
    ]);
  }, []);
  const agentRegistry = useAgentRegistry({ onAgentResult: handleAgentResult });
  const activeAgentCount = agentRegistry.active.length;
  const previousActiveAgentCountRef = useRef(activeAgentCount);

  useEffect(() => {
    if (activeAgentCount > 0 && previousActiveAgentCountRef.current === 0) {
      setAgentDrawerExpanded(true);
    }
    previousActiveAgentCountRef.current = activeAgentCount;
  }, [activeAgentCount]);

  const handleToggleAgentDrawer = useCallback(() => {
    setAgentDrawerExpanded(expanded => !expanded);
  }, []);

  // Any modal/overlay open — drives both onboarding pause and hiding the
  // background content (viola-inner-card) from assistive tech + the display
  // scanner while a modal's backdrop visually covers it.
  // menuOpen is deliberately excluded: the dropdown renders INSIDE the
  // inner-card, so making the card inert while the menu is open prevents
  // clicking menu items. The menu is a lightweight dropdown, not a full-screen
  // modal overlay, and does not need aria-hidden/inert on the background.
  // cloudWelcomeOpen / cloudLlmConsentOpen belong here for the same reason
  // every other entry does: both render as SIBLINGS of the inner card (see the
  // render block near CloudWelcome/CloudLlmConsentModal), with the shared
  // Modal's z-index:1000 backdrop covering it. Leaving them out meant a
  // first-visit cloud user had the whole dashboard still exposed to assistive
  // tech and still focusable/clickable behind the backdrop, and the display
  // scanner measured the modal's text against the background it visually
  // covers (#4447: the app-display-integrity nightly reported that pairing as
  // text-overlap/interactive-overlap defects). Their open-state lives here
  // rather than beside their hooks further down purely so this expression can
  // read it — `const` is TDZ-bound, so a declaration below would throw at
  // render. The rest of their wiring stays with useCloudWelcome /
  // useCloudLlmConsent below.
  const [cloudLlmConsentOpen, setCloudLlmConsentOpen] = useState(false);
  const [cloudWelcomeOpen, setCloudWelcomeOpen] = useState(false);
  const anyModalOpen = settingsOpen || queueOpen || historyOpen || roomsOpen || helpOpen
    || bugReportOpen || workbenchPanelOpen || !!loginPromptPayload || !!phoneTosPayload
    || calendarModalOpen || cloudWelcomeOpen || cloudLlmConsentOpen;

  // Pause onboarding when any modal is open
  useEffect(() => {
    if (!onboarding.isOnboarding) return;
    if (anyModalOpen) {
      onboarding.pauseOnboarding();
    } else {
      onboarding.resumeOnboarding();
    }
  }, [anyModalOpen, onboarding]);

  // Close the Settings modal the moment the account step's sign-in succeeds.
  // The account step routes sign-in through the full-screen SettingsModal
  // (z-index 1000); without this, that modal stays open over the next step and
  // its backdrop covers the highlighted push-to-talk button, so the PTT "glows
  // but is not clickable" (the founder-caught bug). A one-shot on the
  // checking/awaiting → signed_in transition also avoids fighting later
  // legitimate settings opens during onboarding (e.g. the BYOK step).
  const prevSignInStatusRef = useRef(null);
  useEffect(() => {
    if (
      onboarding.isOnboarding &&
      onboarding.signInStatus === 'signed_in' &&
      prevSignInStatusRef.current !== 'signed_in' &&
      settingsOpen
    ) {
      setSettingsOpen(false);
    }
    prevSignInStatusRef.current = onboarding.signInStatus;
  }, [onboarding.isOnboarding, onboarding.signInStatus, settingsOpen]);

  // Show onboarding speech in AI response area
  useEffect(() => {
    if (onboarding.isOnboarding && onboarding.phaseContent) {
      setLastResponse(`"${onboarding.phaseContent}"`);
    }
  }, [onboarding.isOnboarding, onboarding.phaseContent]);

  // Keyboard input state
  const [typingInput, setTypingInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);

  const normalizeSettingsTab = useCallback((value) => {
    if (typeof value !== 'string' || !value.trim()) return null;
    const key = value.trim().toLowerCase().replace(/[\s-]+/g, '_');
    return SETTINGS_TAB_ALIASES[key] || key;
  }, []);

  const openSettingsPanel = useCallback((tab, section = null) => {
    setSettingsInitialTab(normalizeSettingsTab(tab) || 'account');
    setSettingsInitialSection(typeof section === 'string' && section.trim() ? section.trim() : null);
    setSettingsOpen(true);
    return true;
  }, [normalizeSettingsTab]);

  // The account panel is where the billing machinery already lives: the
  // upgrade panel with live Stripe checkout when the account has no paid plan,
  // and the billing portal when it does. Routing there beats building a second
  // billing surface inside the response area.
  const handleUpgradeFromCap = useCallback(() => {
    openSettingsPanel('account');
  }, [openSettingsPanel]);

  const handlePaidActionGatePayload = useCallback((rawPayload) => {
    const payload = rawPayload?.payload || rawPayload?.data || rawPayload || {};
    const nested = payload?.data && typeof payload.data === 'object' ? payload.data : {};
    const errorCode = payload.error_code || nested.error_code;
    if (errorCode === 'login_required_for_paid_action') {
      setLoginPromptPayload({ ...nested, ...payload });
      return true;
    }
    if (errorCode === 'phone_tos_required') {
      setPhoneTosPayload({ ...nested, ...payload });
      return true;
    }
    return false;
  }, []);

  useEffect(() => {
    const handleLoginRequired = (event) => {
      setLoginPromptPayload(event.detail || {});
    };
    const handlePhoneTosRequired = (event) => {
      setPhoneTosPayload(event.detail || {});
    };
    window.addEventListener('viola:paid-action-login-required', handleLoginRequired);
    window.addEventListener('viola:phone-tos-required', handlePhoneTosRequired);
    return () => {
      window.removeEventListener('viola:paid-action-login-required', handleLoginRequired);
      window.removeEventListener('viola:phone-tos-required', handlePhoneTosRequired);
    };
  }, []);

  // Voice handling
  useEffect(() => {
    const handleUiAction = (event) => {
      const detail = event.detail || {};
      const action = detail.action;
      const payload = detail.payload || {};

      switch (action) {
        case 'open_rooms_add_speaker': {
          setRoomsInitialTab(payload.rooms_modal_tab || payload.tab || 'add-speaker');
          setRoomsPrefill(payload.prefill || {
            room_name: payload.room_name || payload.target_room || '',
          });
          setRoomsOpen(true);
          break;
        }
        case 'open_settings':
          openSettingsPanel(
            payload.tab || payload.sub_tab || payload.settings_tab,
            payload.section || payload.initial_section,
          );
          break;
        case 'open_music_accounts':
          openSettingsPanel('music', 'music_accounts');
          break;
        case 'open_payment_methods':
          openSettingsPanel('account');
          break;
        case 'open_calendar':
          setBrowserModeActive(false);
          setCalendarModeActive(true);
          setDisplayMode('calendar');
          break;
        case 'open_help':
          setHelpOpen(true);
          break;
        case 'open_memory_panel':
        case 'open_knowledge_inbox':
        case 'open_knowledge_folder':
          setWorkbenchPanelOpen(true);
          break;
        default:
          break;
      }
    };

    window.addEventListener('viola:ui-action', handleUiAction);
    return () => window.removeEventListener('viola:ui-action', handleUiAction);
  }, [openSettingsPanel]);

  const handleCommandResult = useCallback((result) => {
    if (result) {
      // Turn-complete backstop (#1407): the arrival of a command result IS the
      // authoritative "this turn is done" signal for every command path (voice
      // HTTP/WS, typed, music box). Resolve the agent-working indicator here so
      // the pill stops claiming "Working..." the moment the turn completes —
      // instead of depending solely on the terminal `agent_progress` broadcast,
      // which is fire-and-forget post-answer (agent_executor.py schedule_post_
      // answer_coro) and, if dropped, would leave the pill spinning for minutes.
      // The 12s visibility cooldown still runs, but as an icon-only resolved
      // affordance (getAgentPillLabel returns '' when inactive), not a spinner.
      setAgentTaskActive(false);
      setAgentTaskStatus('');
      setAgentTaskPhase('');
      handlePaidActionGatePayload(result);
      // Managed-AI usage cap (C-077). Set on a denied turn, cleared on the
      // first turn that answers normally, so the upgrade route appears exactly
      // while the cap is what is stopping the user.
      setCapDenial(extractCapDenial(result));
      dispatchUiActions(result);
      // Auto-link visibility: surface when this turn ran on the user's
      // desktop instead of the cloud.
      setRelayDeviceName(relayDeviceFromResult(result));
      const responseText = result.data?.message || result.data?.response || result.message || result.response || result.data?.text || '';
      if (responseText) {
        setLastResponse(`"${responseText}"`);
        setChatHistory(prev => [
          ...prev,
          { role: 'assistant', content: responseText, timestamp: new Date().toISOString() }
        ]);
      } else if (result.ok === false) {
        // A failed command carries its text in `error.message`, which the
        // expression above never reads — so a crashed turn used to leave the
        // PREVIOUS answer sitting on screen, indistinguishable from a turn
        // that worked. Say what happened instead.
        const failureText = describeError(result, 'That command did not go through. Please try again.');
        setLastResponse(failureText);
        setChatHistory(prev => [
          ...prev,
          { role: 'assistant', content: failureText, timestamp: new Date().toISOString() }
        ]);
      }
      if (onboarding.isOnboarding) {
        onboarding.onCommandExecuted();
      }
    }
  }, [handlePaidActionGatePayload, onboarding]);

  // Voice handler dispatch:
  //   - Hub (isSpoke=false) uses the HTTP path (/v1/transcribe + /v1/command);
  //     TTS plays via sounddevice and the user hears it on the hub speaker.
  //   - Spoke (isSpoke=true) uses /ws/voice-stream so the hub's synthesized
  //     TTS PCM streams back to the originating spoke directly — the
  //     HTTP-only path silently drops audio responses because ProcTap is
  //     locked to the music-backend PID during playback.
  // Both hooks are mounted unconditionally to honour the rules of hooks;
  // neither acquires the mic, opens a WS, or creates an AudioContext until
  // its own startRecording() is invoked, so the inactive one is free.
  // During onboarding, only the merged "mic_try" step should actually run a
  // command (its whole point is the first-answer aha). Every other onboarding
  // phase records the transcript for the mic check but must not execute a
  // command if the user happens to press PTT. Outside onboarding, always execute.
  const executeCommandFlag = !onboarding.isOnboarding || onboarding.phase === 'mic_try';
  // Browser hands-free wake word (opt-in, device-local, client-side). Declared
  // here so the persistent wake mic stream can be reused by the voice turn.
  const [handsFreeWake] = useHandsFreeWake();
  const [wakeStream, setWakeStream] = useState(null);
  const httpVoice = useVoice(handleCommandResult, {
    existingStream: micStream,
    executeCommand: executeCommandFlag,
  });
  const wsVoice = useVoiceWs(handleCommandResult, {
    room: room || 'speaker',
    // Reuse the already-hot wake mic (if any) so a wake turn does not trigger a
    // second getUserMedia; falls back to the caller-provided micStream / own.
    existingStream: micStream || wakeStream,
    executeCommand: executeCommandFlag,
  });
  // A plain cloud browser has no desktop-local sounddevice and 404s on
  // /v1/transcribe, so it must use the WebSocket voice pipe (in-process
  // STT -> agent -> TTS streamed back and played via Web Audio), exactly like a
  // multiroom spoke. The desktop hub (isCloudSurface() === false) keeps the HTTP
  // path so it plays TTS on its own speaker. Spokes are unchanged.
  const voice = (isSpoke || isCloudSurface()) ? wsVoice : httpVoice;

  // Cloud browser first-run consent for the managed LLM. Enabled only on the
  // cloud surface so the desktop hub never touches the cloud consent endpoint.
  const cloudSurfaceActive = useMemo(() => isCloudSurface(), []);
  const cloudLlmConsent = useCloudLlmConsent({ enabled: cloudSurfaceActive });
  // What the user was trying to do when the consent prompt interrupted them, so
  // the turn they actually asked for resumes after they accept (their voice
  // turn, or the text they had already typed) instead of being dropped.
  const pendingConsentActionRef = useRef(null);

  // THE gate. Every way of starting an agent turn calls this first and returns
  // early when it says true; nothing else evaluates the consent rule (#362).
  //
  // It is published on CloudConsentGateContext as well as used here, because
  // turn entry points that live in their own components (the chat-mode
  // composer) cannot see this component's state and therefore could not use the
  // shared predicate at all -- which is how a second ungated turn surface
  // shipped immediately after the first one was fixed.
  // The consent value is read through `readGranted()` (a ref), NOT the `granted`
  // state snapshot, because accepting the prompt resumes the interrupted turn in
  // the SAME tick the grant lands: a state snapshot is still pre-grant there, so
  // the resumed turn re-intercepts and re-opens the prompt the user just
  // accepted, forever (#4785, reproduced live on prod ca19433c9 - all three
  // consent writes returned 200 and the modal came back twice out of two).
  const readCloudLlmConsent = cloudLlmConsent.readGranted;
  const interceptCloudConsent = useCallback((pendingAction) => {
    if (!shouldPromptForCloudConsent(cloudSurfaceActive, readCloudLlmConsent())) return false;
    pendingConsentActionRef.current = pendingAction || { kind: 'voice' };
    setCloudLlmConsentOpen(true);
    return true;
  }, [cloudSurfaceActive, readCloudLlmConsent]);

  // Every text command from the composer goes through here so the SAME cloud
  // first-run consent gate that guards push-to-talk also guards typing (#362).
  // Without this a brand-new cloud user who typed instead of speaking was never
  // prompted at all: the command hit `can_execute_cloud_agent`, was refused for
  // the consents they had no way to grant, and the stage rendered the pipeline's
  // "No handler matched your request" fallback. Verified live on deployed SHA
  // 0d13cdf2a with a freshly signed-up marked account, 2026-08-04.
  const submitTextCommand = useCallback((text) => {
    if (interceptCloudConsent({ kind: 'text', text })) return Promise.resolve();
    return api.sendCommand(text).then(handleCommandResult);
  }, [interceptCloudConsent, api, handleCommandResult]);

  // Cloud browser first-visit welcome (#2607): server-side completion-gated
  // (useCloudWelcome -> /v1/cloud-welcome/*), so it opens once `completed`
  // resolves to explicitly false (never on the null "still loading" window,
  // never for a returning completed user) and is never shown again once
  // finished or skipped.
  const cloudWelcome = useCloudWelcome({ enabled: cloudSurfaceActive });
  useEffect(() => {
    if (!cloudSurfaceActive || cloudWelcome.completed !== false) return undefined;
    const timer = setTimeout(() => setCloudWelcomeOpen(true), 400);
    return () => clearTimeout(timer);
  }, [cloudSurfaceActive, cloudWelcome.completed]);
  const handleCloudWelcomeFinish = useCallback(async () => {
    await cloudWelcome.complete();
    setCloudWelcomeOpen(false);
  }, [cloudWelcome]);
  const handleCloudWelcomeSkip = useCallback(async () => {
    await cloudWelcome.complete();
    setCloudWelcomeOpen(false);
  }, [cloudWelcome]);

  // CONFIRM-6 warm-keeping: speculative warm-up on an intent signal
  // (mic-button hover/focus). Never touches the mic itself — only primes the
  // shared TTS AudioContext and, on the WS voice path, opens the
  // /ws/voice-stream connection ahead of the actual press so the real press
  // can reuse it (see useVoiceWs.prewarmConnection). httpVoice (desktop hub)
  // has no such connection to warm, so this is a harmless no-op there.
  const handleMicIntent = useCallback(() => {
    prewarmTtsContext();
    if (typeof voice.prewarmConnection === 'function') {
      voice.prewarmConnection();
    }
  }, [voice]);

  // CONFIRM-6 warm-keeping: prime the shared TTS AudioContext on the very
  // FIRST user gesture anywhere on the page — not just a mic-button hover —
  // so a user who interacts elsewhere first (dismisses a modal, opens a menu)
  // still has a running (not iOS-suspended) AudioContext by the time they
  // press push-to-talk. Cheap, fires once, no mic access.
  useEffect(() => {
    let done = false;
    const primeOnce = () => {
      if (done) return;
      done = true;
      prewarmTtsContext();
      window.removeEventListener('pointerdown', primeOnce, true);
      window.removeEventListener('keydown', primeOnce, true);
    };
    window.addEventListener('pointerdown', primeOnce, true);
    window.addEventListener('keydown', primeOnce, true);
    return () => {
      window.removeEventListener('pointerdown', primeOnce, true);
      window.removeEventListener('keydown', primeOnce, true);
    };
  }, []);

  // CONFIRM-6 warm-keeping: a returning user who has already granted mic
  // permission is a strong signal they use voice regularly — warm the
  // connection once on mount instead of waiting for a hover/focus that may
  // never come on a touch device. Feature-detected via the Permissions API
  // (unsupported for "microphone" on Safari, where this degrades to a no-op
  // and hover/press still work normally). Cloud-surface only, matching where
  // useVoiceWs (and its prewarmConnection) is actually used.
  useEffect(() => {
    if (!cloudSurfaceActive) return undefined;
    if (typeof navigator === 'undefined' || !navigator.permissions
        || typeof navigator.permissions.query !== 'function') {
      return undefined;
    }
    let cancelled = false;
    navigator.permissions.query({ name: 'microphone' }).then((status) => {
      if (cancelled) return;
      if (status.state === 'granted') {
        handleMicIntent();
      }
    }).catch(() => { /* Permissions API has no "microphone" descriptor on this browser */ });
    return () => { cancelled = true; };
  }, [cloudSurfaceActive, handleMicIntent]);

  // Browser hands-free wake word engine (in-tab ONNX via WASM). Runs ONLY on
  // surfaces that use the WS voice pipe (spoke / cloud browser) — the desktop
  // hub has the native ViolaWake engine. `onWake` dispatches through a ref
  // because beginVoiceTurn is defined later in the component; the assignment
  // effect below keeps it current. Wake threshold 0.90 is canon.
  const browserWakeActionRef = useRef(null);
  const browserWakeOnWake = useCallback(() => {
    if (browserWakeActionRef.current) browserWakeActionRef.current();
  }, []);
  // Honor the user's Wake-Up Sensitivity setting (same slider the desktop
  // engine uses); default is the 0.90 canon threshold.
  const wakeSensitivity = Number(userSettings?.wake_sensitivity);
  const browserWakeThreshold =
    Number.isFinite(wakeSensitivity) && wakeSensitivity > 0 && wakeSensitivity <= 1
      ? wakeSensitivity
      : 0.9;
  const browserWake = useBrowserWakeWord({
    enabled: handsFreeWake && (isSpoke || cloudSurfaceActive),
    // Pause the wake loop while a turn is being captured/processed or the
    // consent modal is up, so the detector can't re-trigger mid-turn.
    paused: voice.isRecording || voice.isProcessing || cloudLlmConsentOpen || cloudWelcomeOpen,
    onWake: browserWakeOnWake,
    onStreamReady: setWakeStream,
    threshold: browserWakeThreshold,
  });
  const browserWakeListening = browserWake.status === 'listening';

  // Voice processing timeout
  const [processingTooLong, setProcessingTooLong] = useState(false);
  const processingTimerRef = useRef(null);
  useEffect(() => {
    if (voice.isProcessing) {
      processingTimerRef.current = setTimeout(() => setProcessingTooLong(true), 30000);
    } else {
      clearTimeout(processingTimerRef.current);
      setProcessingTooLong(false);
    }
    return () => clearTimeout(processingTimerRef.current);
  }, [voice.isProcessing]);

  // Derived from playerState
  const isPlaying = playerState.is_playing;
  const progress = (playerState.position_percentage || 0) * 100;
  const nowPlaying = playerState.now_playing;
  const hasQueuedTracks = Array.isArray(playerState.queue) && playerState.queue.length > 0;
  const canResumePlayback = Boolean(nowPlaying) || hasQueuedTracks;
  const volume = playerState.volume || 80;

  // Sync repeat/shuffle from backend
  useEffect(() => {
    if (playerState.repeat_mode !== undefined) setRepeatMode(playerState.repeat_mode);
    if (playerState.shuffle !== undefined) setShuffleOn(playerState.shuffle);
  }, [playerState.repeat_mode, playerState.shuffle]);

  // Desktop notification on track change
  const prevTrackTitleRef = useRef(null);
  useEffect(() => {
    const title = nowPlaying?.title;
    if (title && title !== prevTrackTitleRef.current && prevTrackTitleRef.current !== null) {
      showNotification('Now Playing', {
        body: `${title}${nowPlaying?.artist ? ' \u2014 ' + nowPlaying.artist : ''}`,
        tag: 'viola-now-playing',
      }, userSettings?.show_notifications ?? true);
    }
    prevTrackTitleRef.current = title || null;
  }, [nowPlaying?.title, nowPlaying?.artist, userSettings?.show_notifications]);

  // =========================================================================
  // DISPLAY MODE DETERMINATION
  // =========================================================================
  useEffect(() => {
    // A live phone call HOLDS the phone tab when the user is already on it, but
    // does NOT auto-open it (founder direction 2026-06-29: placing a call must
    // not switch the stage out from under the user). When the user is on the
    // phone tab during a call, hold it so a concurrent agentic-task/browser
    // signal can't flip the stage mid-call; otherwise leave the stage where the
    // user put it. The live-call screen still renders for a tab opened mid-call
    // via activeCallId-driven recovery (see the fetchActiveCall effect).
    if (activeCallId && getStageContentMode(displayMode) === 'phone') {
      return;
    }
    if (getStageContentMode(displayMode) !== 'music') {
      return;
    }
    if (displayPriorityOverride && agentTaskActive) {
      setDisplayMode(displayPriorityOverride);
      return;
    }
    if (displayPriorityOverride && !agentTaskActive) {
      setDisplayPriorityOverride(null);
    }
    if (agentTaskActive) {
      setDisplayMode('agentic_task');
      return;
    }
    if (calendarModeActive) {
      setDisplayMode('calendar');
      return;
    }
    if (browserModeActive) {
      setDisplayMode('browser');
      return;
    }
    if (playerState.is_playing || playerState.is_paused) {
      setDisplayMode('now_playing');
      return;
    }
    if (displayMode !== 'now_playing') { setDisplayMode('now_playing'); }
  }, [activeCallId, playerState.is_playing, playerState.is_paused, browserModeActive, calendarModeActive, agentTaskActive, displayMode, displayPriorityOverride]);

  // Stage renders all mode panels, so the hidden music panel can keep the
  // YouTube iframe alive without forcing music to be the visible stage.

  // Browser-auth overlays are connection UI, not playback UI. A stale overlay
  // must never cover the YouTube iframe, which is the active playback engine.
  useEffect(() => {
    if (!playerState.now_playing?.video_id || !browserOverlayVisible) return;
    setBrowserOverlayVisible(false);
    if (typeof window !== 'undefined' && window.viola?.hideBrowserOverlay) {
      window.viola.hideBrowserOverlay();
    }
  }, [browserOverlayVisible, playerState.now_playing?.video_id]);

  // Expose browser mode controls to Qt/Python
  useEffect(() => {
    window.__violaSetBrowserMode = (active, url) => {
      setBrowserModeActive(!!active);
      if (url !== undefined) setBrowserUrl(url || '');
      setDisplayMode((currentMode) => {
        if (active) return 'browser';
        return currentMode === 'browser' ? 'now_playing' : currentMode;
      });
    };
    window.__violaSetBrowserUrl = (url) => { setBrowserUrl(url || ''); };
    return () => { delete window.__violaSetBrowserMode; delete window.__violaSetBrowserUrl; };
  }, []);

  // =========================================================================
  // STAGE BROWSER OVERLAY BOUNDS
  // =========================================================================
  const handleStageRectChange = useCallback((rect) => {
    if (typeof window === 'undefined') return;
    // BrowserMode owns the native webview surface rect so React controls stay clickable.
    if (getStageContentMode(displayMode) === 'browser') return;
    if (!window.viola || typeof window.viola.setBrowserOverlayBounds !== 'function') return;
    window.viola.setBrowserOverlayBounds(rect.x, rect.y, rect.width, rect.height);
  }, [displayMode]);

  // Listen for browser_overlay_state WebSocket messages
  useEffect(() => {
    if (!setOverlayCallback) return;
    const handleOverlayState = (msg) => {
      const visible = msg.visible ?? msg.payload?.visible;
      const mode = msg.mode || msg.payload?.mode || '';
      const url = msg.url || msg.payload?.url || '';
      const agentTask = msg.agent_task || msg.payload?.agent_task;
      if (visible !== undefined) { setBrowserOverlayVisible(!!visible); }
      if (mode === 'agentic') {
        setAgentTaskActive(true);
        setBrowserUrl(url);
        if (agentTask) {
          setAgentTaskDescription(agentTask.description || '');
          setAgentTaskStatus(agentTask.status || 'working');
          setAgentTaskPhase(agentTask.phase || 'acting');
          if (agentTask.phase === 'user_input') { setAgentTakeoverActive(true); }
        }
      } else if (!visible) {
        const outcome = msg.agent_outcome || msg.payload?.agent_outcome || '';
        if (outcome === 'done') { addToast({ message: 'Done \u2713', level: 'info' }); }
        else if (outcome === 'error') { addToast({ message: 'That didn\'t work — please try again', level: 'error' }); }
        else if (outcome === 'cancelled') { addToast({ message: 'Stopped', level: 'info' }); }
        setAgentTaskActive(false);
        setAgentTaskDescription('');
        setAgentTaskStatus('');
        setAgentTaskPhase('');
        setAgentTakeoverActive(false);
        setBrowserModeActive(false);
        setBrowserUrl('');
        setDisplayMode((currentMode) => (currentMode === 'browser' || currentMode === 'agentic_task' ? 'now_playing' : currentMode));
      } else {
        setAgentTaskActive(false);
      }
    };
    setOverlayCallback(handleOverlayState);
    return () => setOverlayCallback(null);
  }, [setOverlayCallback, addToast]);

  useEffect(() => {
    if (!setAgentProgressCallback) return;
    const handleAgentProgress = (payload) => {
      const terminal = payload?.terminal === true || ['complete', 'error', 'cancelled'].includes(payload?.status);
      if (terminal) {
        setAgentTaskActive(false);
        setAgentTaskStatus('');
        setAgentTaskPhase('');
        setBrowserUrl('');
        setBrowserModeActive(false);
        setDisplayMode((currentMode) => (currentMode === 'browser' || currentMode === 'agentic_task' ? 'now_playing' : currentMode));
        return;
      }
      const progressText = payload?.progress || payload?.message || '';
      if (progressText) {
        setAgentTaskActive(true);
        setAgentTaskStatus(progressText);
      }
      if (payload?.status) {
        setAgentTaskPhase((currentPhase) => (payload.status === 'working' ? 'acting' : currentPhase || 'acting'));
      }
    };
    setAgentProgressCallback(handleAgentProgress);
    return () => setAgentProgressCallback(null);
  }, [setAgentProgressCallback]);

  useEffect(() => {
    if (agentTaskActive || browserModeActive) {
      setAgentContextPillVisible(true);
      return undefined;
    }
    if (!agentContextPillVisible) return undefined;

    const timeoutId = window.setTimeout(() => {
      setAgentContextPillVisible(false);
    }, AGENT_CONTEXT_PILL_COOLDOWN_MS);
    return () => window.clearTimeout(timeoutId);
  }, [agentContextPillVisible, agentTaskActive, browserModeActive]);

  // Display priority override
  useEffect(() => {
    if (!setDisplayPriorityCallback) return;
    const handleDisplayPriority = (payload) => {
      const mode = payload.mode || 'auto';
      if (mode === 'auto') { setDisplayPriorityOverride(null); }
      else if (mode === 'now_playing' || mode === 'agentic_task' || mode === 'browser') { setDisplayPriorityOverride(mode); }
    };
    setDisplayPriorityCallback(handleDisplayPriority);
    return () => setDisplayPriorityCallback(null);
  }, [setDisplayPriorityCallback]);

  // Agent frame streaming over the shared /ws/events socket — used by
  // multiroom spokes. Cloud/LAN web clients use the dedicated
  // /ws/agent-browser stream instead (useDedicatedBrowserStream), and the
  // hub's native-webview path keeps the streamed-frame subscription off.
  useEffect(() => {
    if (!streamBrowserView || useDedicatedBrowserStream || !setAgentFrameCallback) return;
    const handleFrame = (arrayBuffer) => {
      const blob = new Blob([arrayBuffer], { type: 'image/jpeg' });
      const newUrl = URL.createObjectURL(blob);
      setAgentFrameFade(agentFrameUrlRef.current);
      setAgentFrameSrc(newUrl);
      const oldUrl = agentFrameUrlRef.current;
      agentFrameUrlRef.current = newUrl;
      if (oldUrl) { setTimeout(() => URL.revokeObjectURL(oldUrl), 300); }
    };
    setAgentFrameCallback(handleFrame);
    return () => {
      setAgentFrameCallback(null);
      if (agentFrameUrlRef.current) { URL.revokeObjectURL(agentFrameUrlRef.current); agentFrameUrlRef.current = null; }
    };
  }, [streamBrowserView, useDedicatedBrowserStream, setAgentFrameCallback]);

  // Clean up agent frame when agent task ends
  useEffect(() => {
    if (!agentTaskActive && agentFrameSrc) {
      if (agentFrameUrlRef.current) { URL.revokeObjectURL(agentFrameUrlRef.current); agentFrameUrlRef.current = null; }
      setAgentFrameSrc(null);
      setAgentFrameFade(null);
    }
  }, [agentTaskActive, agentFrameSrc]);

  // Track rating
  const [rating, setRating] = useState(null);

  // Wake detector status
  const [wakeDetectorRunning, setWakeDetectorRunning] = useState(false);
  useEffect(() => {
    const checkWakeStatus = async () => {
      try {
        const base = window.__VIOLA_BASE_URL__ || window.location.origin;
        const response = await fetch(`${base}/health`);
        if (response.ok) {
          const data = await response.json();
          setWakeDetectorRunning(data?.dependencies?.wake_detector?.is_running ?? false);
        }
      } catch (e) { /* silently ignore */ }
    };
    checkWakeStatus();
    const interval = setInterval(checkWakeStatus, 3000);
    return () => clearInterval(interval);
  }, []);

  // =========================================================================
  // TOAST NOTIFICATION WIRING
  // =========================================================================
  useEffect(() => {
    if (!setErrorCallback) return;
    const handleError = (payload) => {
      const message = payload.user_message || payload.message || 'An unexpected error occurred.';
      const level = payload.level || 'error';
      addToast({ message, level });
    };
    setErrorCallback(handleError);
    return () => setErrorCallback(null);
  }, [setErrorCallback, addToast]);

  useEffect(() => {
    if (!setDisconnectCallback) return;
    const handleDisconnect = () => { addToast({ message: 'Connection lost. Reconnecting...', level: 'warning' }); };
    setDisconnectCallback(handleDisconnect);
    return () => setDisconnectCallback(null);
  }, [setDisconnectCallback, addToast]);

  // =========================================================================
  // CHAT RESPONSE WIRING
  // =========================================================================
  useEffect(() => {
    if (!setChatResponseCallback) return;
    const handleChatResponse = (payload) => {
      dispatchUiActions(payload);
      const text = payload.text || '';
      if (!text) return;

      const hasExplicitCard = payload.card && typeof payload.card === 'object';
      const isLongResponse = text.length > 200;
      const willShowCard = hasExplicitCard || isLongResponse;

      if (willShowCard) {
        // Card responses: the card is the display, but the response line still
        // shows what Viola actually SAID — same shape as the short-response
        // branch below — so it stays true and readable after the card
        // auto-dismisses. It must never show `payload.intent`: that is the
        // pipeline's own intent slug (`answer`, `set_volume`, `skip`, from
        // core/voice_command_handler.py), not display copy, so it rendered
        // as the literal sentence "Showing details for answer" on screen.
        setLastResponse(`"${truncateForResponseLine(text)}"`);
        if (hasExplicitCard) {
          setActiveCard(payload.card);
        } else {
          // An auto-promoted long answer has no title anyone wrote for it.
          // ContentCard omits the whole title block when `title` is absent,
          // and no title beats a slug titled "answer".
          setActiveCard({ type: 'detail', body: text });
        }
        // Don't add to chatHistory — card is the display
      } else {
        // Short responses: show in chat only, no card
        setLastResponse(`"${text}"`);
        setChatHistory(prev => {
          const last = prev.length > 0 ? prev[prev.length - 1] : null;
          if (last && last.role === 'assistant' && last.content === text) { return prev; }
          return [...prev, { role: 'assistant', content: text, timestamp: new Date().toISOString() }];
        });
      }
    };
    setChatResponseCallback(handleChatResponse);
    return () => setChatResponseCallback(null);
  }, [setChatResponseCallback]);

  useEffect(() => {
    if (!setCalendarUpdateCallback) return;
    const handleCalendarUpdate = () => { window.dispatchEvent(new Event('viola:calendar-updated')); };
    setCalendarUpdateCallback(handleCalendarUpdate);
    return () => setCalendarUpdateCallback(null);
  }, [setCalendarUpdateCallback]);

  // =========================================================================
  // IFRAME COMMUNICATION STATE
  // =========================================================================
  const [iframePosition, setIframePosition] = useState(0);
  const [iframeDuration, setIframeDuration] = useState(0);
  const spokeEmbedReadyRef = useRef(false);
  const spokeInitialSyncDoneRef = useRef(false);
  const iframeRef = useRef(null);
  const skipDebounceRef = useRef(false);
  const skipCountRef = useRef({ count: 0, resetTime: 0 });
  const iframePositionRef = useRef(0);
  const iframeDurationRef = useRef(0);
  const lastPositionUpdateRef = useRef(0);
  const ytHubMutedRef = useRef(false);
  // Which video we have already told the backend failed to start. The player
  // re-posts yt_iframe_autoplay_blocked once a second forever once it exhausts
  // its retries (measured: 11 posts in 15s, and it never clears _wantsToPlay),
  // so the report has to be deduped per video or it becomes a 1 Hz broadcast.
  const notStartedReportedRef = useRef(null);

  // Wake word status.
  //
  // `userSettings` is empty until the settings fetch resolves. Comparing the
  // loaded value alone would silently mean "not wake_word" during that window,
  // so a wake-enabled install reports wake as OFF for the first paint. An absent
  // value falls back to the shipped default (config/defaults.py
  // DEFAULT_VOICE_MODE) instead, which is what the install will actually be.
  const wakeWordEnabled = (userSettings?.voice_mode ?? 'wake_word') === 'wake_word';
  const wakeStatus = (() => {
    if (voice.isRecording) return 'recording';
    if (voice.isProcessing) return 'processing';
    if (voiceStatus?.degraded) return 'degraded';
    if (wakeDetectorRunning || browserWakeListening) return 'wake_listening';
    return wakeWordEnabled ? 'enabled' : 'off';
  })();

  // =========================================================================
  // IFRAME CONTROL HELPERS
  // =========================================================================
  // Sends a player command to the embedded video iframe. Two DISTINCT
  // protocols — do not collapse them (Error 150 regression class, see
  // YouTubeEmbed above):
  //   - Hub: same-origin local iframe (youtube_iframe_v3.html) speaking
  //     `{type:'control', command}` at window.location.origin.
  //   - Spoke: the configured HTTPS helper speaking the `viola_*`
  //     protocol (ViolaWebsite/js/embed.js), targeted at that origin — a
  //     window.location.origin target makes the browser silently drop every
  //     message (shipped exactly that way after 7908fccf deleted this half).
  const sendToIframe = useCallback((command, data = {}) => {
    if (!iframeRef.current?.contentWindow) return;
    try {
      if (isSpoke) {
        let msg;
        switch (command) {
          case 'pause': msg = { type: 'viola_pause' }; break;
          case 'play': msg = { type: 'viola_resume' }; break;
          case 'seekTo': msg = { type: 'viola_seek', position: Math.max(0, Math.round(data.seconds || 0)) }; break;
          case 'setVolume': msg = { type: 'viola_volume', level: data.level || 0 }; break;
          case 'loadVideo': msg = { type: 'viola_play', videoId: data.videoId, startAt: data.startAt || 0 }; break;
          default: return;
        }
        iframeRef.current.contentWindow.postMessage(msg, SPOKE_EMBED_ORIGIN);
      } else {
        iframeRef.current.contentWindow.postMessage({ type: 'control', command, ...data }, window.location.origin);
      }
      if (import.meta.env.DEV) { console.log('[DIAG] Sent to iframe:', command); }
    } catch (e) {
      if (import.meta.env.DEV) { console.warn('[DIAG] Failed to send to iframe:', e); }
    }
  }, [isSpoke]);

  const rememberCallMeta = useCallback((payload, callId, markStarted = false) => {
    if (!callId) return;
    setActiveCallMeta((current) => {
      const next = {
        ...(current || {}),
        ...(payload || {}),
        call_id: callId,
      };
      if (markStarted && !next.started_at) {
        next.started_at = new Date().toISOString();
      }
      return next;
    });
  }, []);

  // Recover the live call when the phone tab is opened mid-call. activeCallId
  // is otherwise only ever set from transient call_started / call_consultation
  // WebSocket events; a tab opened AFTER those fired (the founder-observed
  // symptom — first production call 2026-06-29 showed the history list while a
  // call was live) has no way to learn the call is live. When the user is on
  // the phone tab and we don't already have an active call, ask the backend
  // whether one is live and adopt it so the live-call screen renders.
  useEffect(() => {
    if (!isPhoneMode || activeCallId) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const active = await fetchActiveCall();
        if (cancelled || !active || !active.call_id) return;
        callFallbackStartRef.current = Date.now();
        rememberCallMeta(active, active.call_id, true);
        setActiveCallId(active.call_id);
      } catch (_) {
        // No live call (or transient error): stay on the history list. The tab
        // open already triggers a history fetch, so nothing else to do.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isPhoneMode, activeCallId, rememberCallMeta]);

  const handlePhoneWsMessage = useCallback((data) => {
    if (!data || typeof data !== 'object') return;
    const payload = data.payload || data;
    const callId = payload.call_id || payload.id;

    if (handlePaidActionGatePayload(data)) {
      return;
    }

    if (data.type === 'call_briefing') {
      setCallBriefing(payload);
      rememberCallMeta(payload, callId);
      if (payload.approved && callId) {
        if (callId !== activeCallId) callFallbackStartRef.current = Date.now();
        setActiveCallId(callId);
      }
    } else if (data.type === 'call_consultation') {
      setCallConsultation(payload);
      rememberCallMeta(payload, callId);
      if (callId) {
        if (callId !== activeCallId) callFallbackStartRef.current = Date.now();
        setActiveCallId(callId);
      }
    } else if (data.type === 'call_started' && callId) {
      // Founder direction (2026-06-29, reversed from the 2026-06-24 auto-open):
      // placing a call must NOT auto-open the phone tab or steal the stage. We
      // record the live call (so the pill shows "Live" and the title flips) but
      // leave the user wherever they were. When the user opens the phone tab,
      // the live-call screen renders because activeCallId is set — and a tab
      // opened later in the call recovers it via the fetchActiveCall effect.
      // Music is NOT auto-paused: the user chose not to be on the call screen,
      // so don't stomp their playback. If they open the tab and Listen / Take
      // over, that listen-in path manages its own audio.
      if (callId !== activeCallId) callFallbackStartRef.current = Date.now();
      rememberCallMeta(payload, callId, true);
      setActiveCallId(callId);
      setLastEndedCall(null);
    } else if (data.type === 'call_cost_update' && callId) {
      if (activeCallId && callId !== activeCallId) return;
      rememberCallMeta(payload, callId);
    } else if (data.type === 'recipient_state' && callId) {
      if (activeCallId && callId !== activeCallId) return;
      rememberCallMeta(payload, callId);
    } else if (data.type === 'call_queue_updated') {
      setPhoneCallQueue(Array.isArray(payload.queue) ? payload.queue : []);
    } else if (data.type === 'call_ended') {
      if (callId && activeCallId && callId !== activeCallId) return;
      const endedCall = {
        ...(activeCallMeta || {}),
        ...(payload || {}),
        call_id: callId || activeCallId || payload.call_id,
      };
      setCallBriefing(null);
      setCallConsultation(null);
      setActiveCallId(null);
      setActiveCallMeta(null);
      if (endedCall.call_id) {
        setLastEndedCall(endedCall);
      }
      // Call no longer auto-opened the phone tab (founder direction 2026-06-29),
      // so on end we leave the user's stage exactly where it is and do NOT touch
      // their music. If the user happens to be on the phone tab, clearing
      // activeCallId naturally drops them to the call-history list — the right
      // post-call view for someone already looking at the phone tab.
    }
  }, [
    activeCallId,
    activeCallMeta,
    handlePaidActionGatePayload,
    rememberCallMeta,
  ]);
  useWebSocket(handlePhoneWsMessage);

  // Live cloud-call events (transcript / cost / consult). On a same-origin web
  // client these arrive on the shared useWebSocket /ws/events socket above (its
  // origin IS the cloud). On the desktop they reach the LOCAL /ws/events socket
  // via the server-side cloud-event relay (telephony/phone_cloud_event_relay.py):
  // the desktop must never hold a cloud bearer in the browser (SEC-017), so the
  // LOCAL backend subscribes to the cloud hub server-side (owner-scoped) and
  // republishes the phone events onto the local hub. useCloudPhoneEvents drives
  // that relay's start/stop lifecycle (carrying only the local api key) and routes
  // the relayed frames to these handlers.
  const handleCloudPhoneTranscript = useCallback((entry) => {
    if (pushCallTranscript) pushCallTranscript(entry);
  }, [pushCallTranscript]);
  const handleCloudPhoneMessage = useCallback((data) => {
    if (data?.type === 'call_cost_update' && applyCallCostUpdate) {
      const payload = data.payload || data;
      applyCallCostUpdate(payload);
    }
    handlePhoneWsMessage(data);
  }, [applyCallCostUpdate, handlePhoneWsMessage]);
  useCloudPhoneEvents({
    enabled: isPhoneMode || Boolean(activeCallId),
    onMessage: handleCloudPhoneMessage,
    onTranscript: handleCloudPhoneTranscript,
  });

  // CB-7 FIX: Forward playback commands from backend to YouTube iframe
  useEffect(() => {
    if (!setPlaybackCommandCallback) return;
    const handlePlaybackCommand = (command, payload) => {
      if (command === 'seek' && payload.position !== undefined) {
        sendToIframe('seekTo', { seconds: payload.position });
      }
    };
    setPlaybackCommandCallback(handlePlaybackCommand);
    return () => setPlaybackCommandCallback(null);
  }, [setPlaybackCommandCallback, sendToIframe]);

  // =========================================================================
  // VOLUME SYNC TO IFRAME
  // =========================================================================
  const lastSyncedVolumeRef = useRef(volume);
  useEffect(() => {
    if (volume !== lastSyncedVolumeRef.current) {
      if (import.meta.env.DEV) {
        const debugToken = window.__VIOLA_DEBUG_AUTH_TOKEN__ || '';
        if (debugToken) {
          fetch('/v1/debug/yt-iframe-event', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Debug-Auth-Token': debugToken },
            body: JSON.stringify({ event: 'REACT_VOLUME_SYNC', prevVolume: lastSyncedVolumeRef.current, newVolume: volume, iframeExists: !!iframeRef.current, isSpoke, ytHubMuted: ytHubMutedRef.current, timestamp: Date.now() })
          }).catch(() => {});
        }
      }
      if (isSpoke) { sendToIframe('setVolume', { level: 0 }); }
      else if (ytHubMutedRef.current) { sendToIframe('setVolume', { level: 0 }); }
      else { sendToIframe('setVolume', { level: volume }); }
      lastSyncedVolumeRef.current = volume;
    }
  }, [volume, sendToIframe, isSpoke]);

  // HUB-LOCAL SYNC
  const ytHubMuted = playerState.yt_hub_muted || false;
  useEffect(() => {
    ytHubMutedRef.current = ytHubMuted;
    if (isSpoke) return;
    if (ytHubMuted) {
      sendToIframe('setVolume', { level: 0 });
      const t1 = setTimeout(() => sendToIframe('setVolume', { level: 0 }), 2000);
      const t2 = setTimeout(() => sendToIframe('setVolume', { level: 0 }), 5000);
      return () => { clearTimeout(t1); clearTimeout(t2); };
    }
    sendToIframe('setVolume', { level: volume });
  }, [ytHubMuted, isSpoke, sendToIframe, volume, nowPlaying?.video_id]);

  // DIAGNOSTIC REQUEST HANDLER
  useEffect(() => {
    const handleDiagnosticRequest = (payload) => {
      if (import.meta.env.DEV) { console.log('[DIAG] Processing diagnostic request'); }
      if (iframeRef.current?.contentWindow) {
        try {
          iframeRef.current.contentWindow.postMessage({ type: 'diagnostic_request' }, window.location.origin);
        } catch (e) {
          if (wsSend) { wsSend({ action: 'frontend_diagnostics', payload: { yt_api_loaded: false, player_exists: false, player_state: -1, player_state_name: 'UNKNOWN', iframe_in_dom: !!iframeRef.current, iframe_visible: false, error: 'Failed to communicate with iframe: ' + e.message, source: 'react_fallback', react_iframe_ref_exists: !!iframeRef.current, react_now_playing_video_id: playerState?.now_playing?.video_id, react_is_playing: playerState?.is_playing, collected_at: Date.now() / 1000 } }); }
        }
      } else {
        if (wsSend) { wsSend({ action: 'frontend_diagnostics', payload: { yt_api_loaded: false, player_exists: false, player_state: -1, player_state_name: 'NO_IFRAME', iframe_in_dom: false, iframe_visible: false, source: 'react_no_iframe', react_iframe_ref_exists: false, react_now_playing_video_id: playerState?.now_playing?.video_id, react_is_playing: playerState?.is_playing, collected_at: Date.now() / 1000 } }); }
      }
    };
    if (setDiagnosticRequestCallback) { setDiagnosticRequestCallback(handleDiagnosticRequest); }
    return () => { if (setDiagnosticRequestCallback) { setDiagnosticRequestCallback(null); } };
  }, [setDiagnosticRequestCallback, wsSend, playerState?.now_playing?.video_id, playerState?.is_playing]);

  // Derived display values
  const isYouTubeVideo = !!playerState?.now_playing?.video_id;
  // Truth invariant (#1407): with no track loaded the transport shows "Nothing
  // playing", so position/duration/progress MUST read 0 — the backend leaves the
  // last track's position/duration in its state broadcast after a stop, which
  // otherwise left the progress bar with a stale near-full fill.
  const { displayPosition, displayDuration, displayProgress } = computePlayerDisplayMetrics({
    hasTrack: !!nowPlaying,
    isYouTubeVideo,
    isSpoke,
    iframePosition,
    iframeDuration,
    position: playerState.position,
    duration: playerState.duration,
  });

  // =========================================================================
  // HANDLERS
  // =========================================================================
  const handlePlayPause = useCallback(() => {
    if (isPlaying) {
      setLocalIsPlaying(false);
      api.pause();
      sendToIframe('pause');
    } else if (!canResumePlayback) {
      // Nothing loaded and nothing queued — tell the user instead of doing
      // nothing silently (2026-07-10 UX audit, #774).
      addToast({ message: 'Nothing queued yet — ask me to play something first.', level: 'info' });
    } else {
      api.resume().then(() => { setLocalIsPlaying(true); sendToIframe('play'); }).catch((err) => {
        const message = err?.code === 'nothing_to_resume'
          ? 'Nothing queued yet — ask me to play something first.'
          : "Couldn't resume playback. Please try again.";
        addToast({ message, level: 'error' });
      });
    }
  }, [isPlaying, api, sendToIframe, setLocalIsPlaying, canResumePlayback, addToast]);

  // Every transport control fires its request and moves on, so the rejection
  // needs an owner or it escapes to `window.onerror` as an uncaught error that
  // no surface ever renders — the user clicks and simply nothing happens. That
  // is exactly what the nightly chaos spec caught (CI run 31299805341): with
  // the network HEALTHY, /v1/skip answered 409 empty_queue and /v1/previous
  // answered "no previous track", and both rejections went unhandled.
  const notifyTransportFailure = useCallback((err, copy) => {
    const toast = describeTransportFailure(err, copy);
    if (toast) addToast(toast);
  }, [addToast]);

  const handleNext = useCallback(() => {
    if (skipDebounceRef.current) return;
    skipDebounceRef.current = true;
    api.skip()
      // `.finally()` does NOT handle a rejection — it re-raises it. The catch
      // has to come first, or the debounce reset alone leaves the rejection
      // unowned (the pre-fix shape).
      .catch((err) => notifyTransportFailure(err, {
        refused: 'No more tracks in the queue.',
        failed: "Couldn't skip to the next track. Please try again.",
      }))
      .finally(() => { setTimeout(() => { skipDebounceRef.current = false; }, 500); });
  }, [api, notifyTransportFailure]);

  const handlePrevious = useCallback(() => {
    api.previous().catch((err) => notifyTransportFailure(err, {
      refused: "You're already at the first track.",
      failed: "Couldn't go back a track. Please try again.",
    }));
  }, [api, notifyTransportFailure]);

  const handleRating = (newRating) => {
    if (!nowPlaying) return;
    const actualRating = rating === newRating ? null : newRating;
    setRating(actualRating);
    // The star flips optimistically, so a failed save must flip it back rather
    // than leave the UI claiming a rating the server never stored (#4214's
    // shape, on the rating control).
    api.setRating(actualRating).catch((err) => {
      setRating(rating);
      notifyTransportFailure(err, {
        refused: "Couldn't rate this track right now.",
        failed: "Couldn't save that rating. Please try again.",
      });
    });
  };

  // Shuffle flips optimistically, so it MUST be able to flip back (#4214).
  // Before the fix this was `setShuffleOn(next); api.setShuffle(next);` with
  // the promise neither awaited nor caught. On any failure the toggle stayed
  // showing the state the user asked for while the server kept the old one,
  // and it could not self-correct: the reconcile effect below only re-runs
  // when the server's value *changes*, which a failed request never does. So
  // the divergence lasted until some other actor moved shuffle.
  const handleShuffleToggle = useCallback(() => {
    const previous = shuffleOn;
    const next = !previous;
    setShuffleOn(next);
    api.setShuffle(next).catch((err) => {
      // Prefer the server's own answer: `/v1/shuffle` reorders before it
      // records, so its error envelope reports the preference actually in
      // effect. Fall back to the pre-toggle value, which is what the server
      // still holds for a network/auth failure that never reached the route.
      const truth = typeof err?.data?.shuffle === 'boolean' ? err.data.shuffle : previous;
      setShuffleOn(truth);
      addToast({ message: "Couldn't change shuffle. Please try again.", level: 'error' });
    });
  }, [shuffleOn, api, setShuffleOn, addToast]);

  // PTT supports tap-vs-hold on the same button:
  //   - tap (press < 250 ms then release): keep recording; client-side VAD
  //     auto-stops after ~1.5 s of silence and submits the turn.
  //   - hold (press >= 250 ms): stops the instant the button is released.
  //   - tapping again while a tap-mode turn is recording stops immediately.
  // The audio ducker is engaged in voice.startRecording (/v1/audio/duck) and
  // released in voice.stopRecording (/v1/audio/unduck), so ducking covers
  // the entire turn regardless of which path ends it.
  const pttPressStartRef = useRef(0);
  const TAP_THRESHOLD_MS = 250;

  const beginVoiceTurn = useCallback(() => {
    pttPressStartRef.current = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    voice.startRecording();
    setChatHistory(prev => [...prev, { role: 'user', content: '(listening...)', timestamp: new Date().toISOString(), pending: true }]);
  }, [voice]);

  const handlePTTStart = useCallback(() => {
    // Muted mic hard-gates PTT too: a hotkey/tray mute is meant to stop the
    // mic entirely, so a stray push-to-talk press while muted must not
    // start a recording.
    if (userSettings?.mic_muted) {
      // Returning silently made a muted mic look like a broken button: every
      // press did nothing, with no state change and no message anywhere (the
      // only mute indicator lives inside the Settings modal). Say why.
      addToast({ message: 'Your microphone is muted. Unmute it to talk to Viola.', level: 'warning' });
      return;
    }
    // Cloud browser first-run gate: a new cloud user has not consented yet, so
    // the managed agent is disabled and turns fall back silently. Intercept the
    // first push-to-talk and ask for consent instead of starting a dead turn.
    // The rule itself lives in the shared gate so every entry point shares it.
    if (!voice.isRecording && interceptCloudConsent({ kind: 'voice' })) {
      return;
    }
    if (voice.isRecording) {
      pttPressStartRef.current = 0;
      voice.stopRecording();
      return;
    }
    // #385: the prior turn is still in flight (processing, or tearing down after
    // command_result while its TTS plays). useVoiceWs would silently drop a
    // startRecording() here (sessionActiveRef guard), so DON'T dispatch a dead
    // turn or append a phantom "(listening...)" bubble that never resolves — the
    // busy state is already visible on the PTT surface (disabled + "responding").
    if (voice.isBusy) {
      pttPressStartRef.current = 0;
      return;
    }
    beginVoiceTurn();
  }, [voice, beginVoiceTurn, interceptCloudConsent, userSettings?.mic_muted, addToast]);

  const handlePTTEnd = useCallback(() => {
    const pressedAt = pttPressStartRef.current;
    if (pressedAt === 0) return;
    const now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    pttPressStartRef.current = 0;
    if (now - pressedAt < TAP_THRESHOLD_MS) {
      return;
    }
    voice.stopRecording();
  }, [voice]);

  const handleCloudLlmConsentAccept = useCallback(async () => {
    // A partial grant leaves the agent just as blocked, so `grant()` reports
    // false and the prompt stays open for a retry rather than resuming a turn
    // that the server would refuse again.
    const ok = await cloudLlmConsent.grant();
    if (!ok) return;
    setCloudLlmConsentOpen(false);
    // Consent granted: proceed with the turn the user originally tried to start,
    // which is a typed command just as often as a voice turn.
    const pending = pendingConsentActionRef.current;
    pendingConsentActionRef.current = null;
    // A turn started from another component (the chat composer) knows how to
    // resume itself and this one does not, so it hands over a resume callback
    // rather than a payload this component would have to re-dispatch blindly.
    if (pending && typeof pending.resume === 'function') {
      void Promise.resolve(pending.resume()).catch(() => {});
      return;
    }
    if (pending && pending.kind === 'text') {
      void api.sendCommand(pending.text).then(handleCommandResult);
      return;
    }
    beginVoiceTurn();
  }, [cloudLlmConsent, beginVoiceTurn, api, handleCommandResult]);

  // Keep the browser-wake action current. On an in-tab wake detection, start a
  // voice turn EXACTLY like push-to-talk does — same consent gate, same
  // beginVoiceTurn — so the server sees an indistinguishable ptt_start turn.
  useEffect(() => {
    browserWakeActionRef.current = () => {
      // #385: isBusy spans the whole turn incl. the post-command_result TTS
      // window where isProcessing has already cleared; a wake fired then would
      // be silently dropped by the sessionActiveRef guard, so gate on it too.
      if (voice.isRecording || voice.isProcessing || voice.isBusy) return;
      if (interceptCloudConsent({ kind: 'voice' })) return;
      beginVoiceTurn();
    };
  }, [voice.isRecording, voice.isProcessing, voice.isBusy, interceptCloudConsent, beginVoiceTurn]);

  // Whenever a final transcript lands (hold release OR VAD-driven tap end),
  // resolve any pending placeholder entry in the chat history and surface
  // onboarding mic-test feedback. This runs for both PTT paths.
  const lastResolvedTranscriptRef = useRef('');
  useEffect(() => {
    const text = voice.transcript;
    if (!text || text === lastResolvedTranscriptRef.current) return;
    lastResolvedTranscriptRef.current = text;
    setChatHistory(prev => {
      const updated = [...prev];
      const lastIndex = updated.findLastIndex(m => m.pending);
      if (lastIndex >= 0) {
        updated[lastIndex] = { ...updated[lastIndex], content: text, pending: false };
        return updated;
      }
      return [...updated, { role: 'user', content: text, timestamp: new Date().toISOString() }];
    });
    if (onboarding.isOnboarding) { onboarding.onMicTestResult(text); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.transcript]);

  // Time update
  useEffect(() => {
    const timer = setInterval(() => setCurrentTime(new Date()), 10000);
    return () => clearInterval(timer);
  }, []);

  // =========================================================================
  // IFRAME MESSAGE LISTENER
  // =========================================================================
  useEffect(() => {
    // Report the player's real UNSTARTED state once per video. The backend's
    // youtube_state handler only advances the queue on ENDED, so this corrects
    // the reported is_playing without touching the skip policy (#2757).
    const reportPlaybackDidNotStart = (videoId) => {
      const key = videoId || '__unknown_video__';
      if (notStartedReportedRef.current === key) return;
      notStartedReportedRef.current = key;
      if (!wsSend) return;
      wsSend({
        action: 'youtube_state',
        payload: {
          state: 'UNSTARTED',
          position: 0,
          duration: iframeDurationRef.current,
          video_id: videoId,
        },
      });
    };

    const handleIframeMessage = (event) => {
      // SPOKE MODE: the video iframe is the cross-origin
      // configured helper page. Bind replies to both its origin and the exact
      // iframe window; another same-origin frame cannot impersonate the player.
      if (isSpoke) {
        if (!isYouTubeEmbedMessage(event, iframeRef.current?.contentWindow, SPOKE_EMBED_URL)) return;
        const spokeMsg = event.data || {};
        if (typeof spokeMsg.type !== 'string' || !spokeMsg.type.startsWith('viola_')) return;
        if (spokeMsg.type === 'viola_position') {
          if (typeof spokeMsg.position === 'number') {
            iframePositionRef.current = spokeMsg.position;
            setIframePosition(spokeMsg.position);
          }
          if (typeof spokeMsg.duration === 'number') {
            iframeDurationRef.current = spokeMsg.duration;
            setIframeDuration(spokeMsg.duration);
          }
        } else if (spokeMsg.type === 'viola_state') {
          if (spokeMsg.state === 'ENDED') {
            iframePositionRef.current = 0;
            setIframePosition(0);
          }
        } else if (spokeMsg.type === 'viola_error') {
          if (import.meta.env.DEV) { console.warn('[DIAG] Spoke embed error:', spokeMsg.code, spokeMsg.name); }
        } else if (spokeMsg.type === 'viola_ready') {
          spokeEmbedReadyRef.current = true;
          // Spoke video is ALWAYS muted — the hub's PCM stream is the audio.
          sendToIframe('setVolume', { level: 0 });
          const videoId = playerState?.now_playing?.video_id;
          if (videoId) {
            // iOS autoplay recovery (original e00758cb): Safari can refuse the
            // embed's own muted autoplay; re-sending viola_play + play/pause
            // from the parent once the player reports ready nudges it into the
            // correct state. Fires once per player creation (viola_ready is
            // emitted only from the embed's onReady), never on a cadence.
            const hubPos = playerState.position || 0;
            const pipelineDelay = window.__spokeEngine?.getBufferTarget().sec ?? 0.14;
            const startAt = hubPos > 2 ? Math.max(0, Math.round(hubPos - pipelineDelay)) : 0;
            sendToIframe('loadVideo', { videoId, startAt });
            sendToIframe(playerState.is_playing ? 'play' : 'pause');
          }
        }
        return;
      }
      if (event.origin !== window.location.origin) return;
      const msgData = event.data || {};
      const { type, payload } = msgData;
      if (!type) return;
      if (type === 'yt_iframe_ready') {
        // Hub local iframe announce; the spoke's cross-origin embed announces
        // via viola_ready above (a spoke never mounts the local iframe).
        return;
      }
      if (!payload) return;
      if (type === 'yt_diagnostics') {
        if (wsSend) { wsSend({ action: 'frontend_diagnostics', payload: { ...payload, source: 'youtube_iframe', react_iframe_ref_exists: !!iframeRef.current, react_now_playing_video_id: playerState?.now_playing?.video_id, react_is_playing: playerState?.is_playing } }); }
        return;
      }
      if (type === 'yt_iframe_autoplay_blocked') {
        // The player exhausted its play retries and gave up, so nothing is
        // playing. Nothing listened for this before, which left the backend
        // reporting playback that had never started (#2757).
        if (isSpoke) return;
        reportPlaybackDidNotStart(playerState?.now_playing?.video_id);
        return;
      }
      if (type === 'yt_iframe_time') {
        const position = payload.currentTime || 0;
        const duration = payload.duration || 0;
        iframePositionRef.current = position;
        iframeDurationRef.current = duration;
        setIframePosition(position);
        setIframeDuration(duration);
        const now = Date.now();
        if (now - lastPositionUpdateRef.current >= 1000) {
          lastPositionUpdateRef.current = now;
          if (wsSend) { wsSend({ action: 'position_update', payload: { position, duration, state: payload.state, source: 'youtube_iframe' } }); }
        }
      }
      if (type === 'yt_iframe_state') {
        const ytState = payload.state;
        const stateVideoId = payload.videoId;
        const currentVideoId = playerState?.now_playing?.video_id;
        if (stateVideoId && currentVideoId && stateVideoId !== currentVideoId) return;
        const stateMap = { '-1': 'UNSTARTED', '0': 'ENDED', '1': 'PLAYING', '2': 'PAUSED', '3': 'BUFFERING', '5': 'CUED' };
        const stateString = stateMap[String(ytState)] || 'UNKNOWN';
        if (wsSend) { wsSend({ action: 'youtube_state', payload: { state: stateString, position: iframePositionRef.current, duration: iframeDurationRef.current, video_id: currentVideoId } }); }
        if (ytState === 1 && ytHubMutedRef.current && !isSpoke) { sendToIframe('setVolume', { level: 0 }); }
        if (ytState === 1 && isSpoke) { sendToIframe('setVolume', { level: 0 }); }
        if (ytState === 0) {
          if (import.meta.env.DEV) {
            const debugToken = window.__VIOLA_DEBUG_AUTH_TOKEN__ || '';
            if (debugToken) {
              fetch('/v1/debug/yt-track-ended', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Debug-Auth-Token': debugToken },
                body: JSON.stringify({ video_id: currentVideoId })
              }).catch(() => {});
            }
          }
          if (!(playerState?.queue?.length > 0)) { setIframePosition(0); }
        }
      }
      if (type === 'yt_iframe_error') {
        const errorCode = payload.error;
        const isFatal = payload.fatal;
        const errorVideoId = payload.videoId;
        const currentVideoId = playerState?.now_playing?.video_id;
        if (errorVideoId && currentVideoId && errorVideoId !== currentVideoId) return;
        console.error('[DIAG] YouTube iframe error:', errorCode, 'fatal:', isFatal);
        if (isSpoke) return;
        // Tell the backend playback did not start. The player never reaches
        // PLAYING after a fatal embed error, so it never emits a yt_iframe_state
        // transition either - the backend's optimistic is_playing from the play
        // call would otherwise stand until the embedded watchdog fires (track
        // duration + 60s, or a full 10 minutes when duration is unknown, which
        // it is when the embed never loaded). UNSTARTED is literally the
        // player's reported state at this point. Sent for 101/150 too: those
        // deliberately do not auto-skip, which is exactly why their stale
        // "now playing" would otherwise linger longest (#2757).
        reportPlaybackDidNotStart(errorVideoId || currentVideoId);
        if ([101, 150].includes(errorCode)) return;
        if (isFatal && !skipDebounceRef.current) {
          const now = Date.now();
          const skipData = skipCountRef.current;
          if (now - skipData.resetTime > 10000) { skipData.count = 0; skipData.resetTime = now; }
          if (skipData.count >= 3) return;
          skipData.count++;
          skipDebounceRef.current = true;
          api.skip()
            // Automatic recovery, not something the user asked for, so a failed
            // auto-skip gets no toast — but it still needs an owner or it
            // escapes as an uncaught error. `apiFetch` already console.warn's
            // the status and body, and the stuck track is visible on its own.
            .catch(() => {})
            .finally(() => { setTimeout(() => { skipDebounceRef.current = false; }, 1000); });
        }
      }
    };
    window.addEventListener('message', handleIframeMessage);
    return () => window.removeEventListener('message', handleIframeMessage);

  }, [api, isSpoke, playerState?.is_playing, playerState?.now_playing?.video_id, playerState.position, playerState?.queue?.length, sendToIframe, wsSend]);

  // Reset iframe state when track changes. spokeEmbedReadyRef deliberately
  // survives a track change: the spoke's embed player object persists across
  // viola_play/loadVideoById and emits viola_ready only once per player
  // creation — clearing the flag here would permanently disable the spoke's
  // seek + drift correction after the first track change.
  useEffect(() => {
    setIframePosition(0); setIframeDuration(0);
    iframePositionRef.current = 0; iframeDurationRef.current = 0; lastPositionUpdateRef.current = 0;
    spokeInitialSyncDoneRef.current = false;
    // A new track gets its own chance to fail; the dedupe is per video.
    notStartedReportedRef.current = null;
  }, [playerState?.now_playing?.video_id]);

  // The hub position as a ref, so interval callbacks (drift correction below)
  // read the CURRENT position instead of a stale render's closure.
  const hubPositionRef = useRef(0);
  useEffect(() => { hubPositionRef.current = playerState.position || 0; }, [playerState.position]);

  // SPOKE MODE: drive track changes into the persistent cross-origin embed.
  // EDGE-TRIGGERED on an actual video_id change (prev-ref comparison) — never
  // re-issued on a cadence. Re-issuing loadVideo/seekTo on a ~1s cadence
  // restarts the cross-origin player continuously (the 6505881a canonical-
  // rewire loop); the only periodic sender is the bounded drift check below.
  const prevSpokeVideoRef = useRef(null);
  useEffect(() => {
    if (!isSpoke) return;
    const videoId = playerState?.now_playing?.video_id;
    if (!videoId) {
      // Non-video track: MediaArea unmounts the embed iframe. Forget the
      // previous video so the next video track (fresh iframe, correct src)
      // is recorded without a redundant viola_play at the new iframe.
      prevSpokeVideoRef.current = null;
      return;
    }
    if (prevSpokeVideoRef.current === null) { prevSpokeVideoRef.current = videoId; return; }
    if (videoId === prevSpokeVideoRef.current) return;
    prevSpokeVideoRef.current = videoId;
    const hubPos = playerState.position || 0;
    const pipelineDelay = window.__spokeEngine?.getBufferTarget().sec ?? 0.14;
    const startAt = hubPos > 2 ? Math.max(0, Math.round(hubPos - pipelineDelay)) : 0;
    sendToIframe('loadVideo', { videoId, startAt });

  }, [isSpoke, playerState?.now_playing?.video_id, playerState.position, sendToIframe]);

  // SPOKE MODE: pause/resume follow the hub. EDGE-TRIGGERED on an actual
  // is_playing transition — never re-issued on a cadence (same loop guard).
  const prevSpokeIsPlayingRef = useRef(null);
  useEffect(() => {
    if (!isSpoke) return;
    const isPlayingNow = !!playerState.is_playing;
    if (prevSpokeIsPlayingRef.current === isPlayingNow) return;
    const isFirstObservation = prevSpokeIsPlayingRef.current === null;
    prevSpokeIsPlayingRef.current = isPlayingNow;
    // First observation and pre-ready toggles are handled by the viola_ready
    // handler, which applies the current is_playing when the player comes up.
    if (isFirstObservation || !spokeEmbedReadyRef.current) return;
    sendToIframe(isPlayingNow ? 'play' : 'pause');
  }, [isSpoke, playerState.is_playing, sendToIframe]);

  // SPOKE MODE: Video position sync — one-time initial seek per track when
  // the hub is already mid-song (join / late mount).
  useEffect(() => {
    if (!isSpoke) return;
    const videoId = playerState?.now_playing?.video_id;
    if (!videoId || !spokeEmbedReadyRef.current || !iframeRef.current?.contentWindow) return;
    const hubPosition = playerState.position || 0;
    const PIPELINE_DELAY_SEC = window.__spokeEngine?.getBufferTarget().sec ?? 0.14;
    if (!spokeInitialSyncDoneRef.current && hubPosition > 5) {
      spokeInitialSyncDoneRef.current = true;
      sendToIframe('seekTo', { seconds: Math.max(0, hubPosition - PIPELINE_DELAY_SEC) });
      return;
    }
    if (!spokeInitialSyncDoneRef.current) { spokeInitialSyncDoneRef.current = true; }

  }, [isSpoke, playerState?.now_playing?.video_id, playerState.position, sendToIframe]);

  // SPOKE MODE: Periodic drift correction — the live-proven bounded shape
  // (5s cadence, >1s threshold; 6505881a documents why tighter loops fail).
  useEffect(() => {
    if (!isSpoke) return;
    const PIPELINE_DELAY_SEC = window.__spokeEngine?.getBufferTarget().sec ?? 0.14;
    const intervalId = setInterval(() => {
      if (!iframeRef.current?.contentWindow || !spokeEmbedReadyRef.current) return;
      const hubPosition = hubPositionRef.current;
      if (hubPosition <= 0) return;
      const targetPosition = Math.max(0, hubPosition - PIPELINE_DELAY_SEC);
      const drift = Math.abs((iframePositionRef.current || 0) - targetPosition);
      if (drift > 1) {
        sendToIframe('seekTo', { seconds: targetPosition });
      }
    }, 5000);
    return () => clearInterval(intervalId);

  }, [isSpoke, sendToIframe]);

  // Fetch weather
  const weatherLocation = userSettings?.weather_location;
  const prevWeatherLocationRef = useRef(undefined);
  useEffect(() => {
    if (settingsLoading) return;
    if (!weatherLocation) {
      setWeatherTemp('--');
      setWeatherDesc('');
      setWeatherData(null);
      setWeatherForecastData(null);
      setWeatherForecastFetchedAt(0);
      setWeatherForecastOpen(false);
      return;
    }
    let weatherTimerId = null;
    let retryTimerId = null;
    let weatherLoaded = false;
    let cancelled = false;
    let failureCount = 0;
    const applyWeatherData = (data) => {
      if (!data || data.temperature === undefined) return false;
      setWeatherData(data);
      if (hasForecastPayload(data)) {
        setWeatherForecastData(data);
        setWeatherForecastFetchedAt(Date.now());
      }
      setWeatherTemp(`${Math.round(data.temperature)}°`);
      setWeatherDesc(describeCondition(data.condition || data.description, data.condition_code));
      // Always assign, including the unknown case. The old keyword chain had no
      // final branch, so a payload it did not recognise left the icon on its
      // previous (or initial partly-cloudy) value: missing data rendered as a
      // confident forecast. (Supersedes an earlier, narrower fix from #3947 that
      // only added a final "cloudy" else-branch; this resolver-based version
      // covers every condition, not just the unmatched case, and renders a
      // dedicated unknown glyph instead of guessing "cloudy".)
      const conditionKey = normalizeConditionKey(data.condition_code, data.condition, data.description);
      const isNight = new Date().getHours() >= 19;
      setWeatherCondition(
        conditionKey === 'clear' && isNight ? 'clear-night' : conditionKey,
      );
      if (data.auto_detected_location) { refreshSettings(); }
      return true;
    };
    const fetchWeather = async (forceRefresh = false) => {
      try {
        const data = await api.getWeather(weatherLocation, { forceRefresh });
        if (!cancelled && applyWeatherData(data)) {
          weatherLoaded = true; failureCount = 0;
          if (retryTimerId) { clearInterval(retryTimerId); retryTimerId = null; }
          if (!weatherTimerId) { weatherTimerId = setInterval(() => fetchWeather(false), 10 * 60 * 1000); }
        }
      } catch (e) {
        if (cancelled || weatherLoaded) return;
        failureCount++;
        if (failureCount >= 3) {
          setWeatherDesc('Weather unavailable'); setWeatherTemp('--');
          // The icon has to fall back too, or a dead topbar keeps showing the
          // last (or initial) glyph and still looks like a live forecast.
          setWeatherCondition(UNKNOWN_CONDITION);
          if (retryTimerId) { clearInterval(retryTimerId); retryTimerId = null; }
          if (!retryTimerId) { retryTimerId = setInterval(() => fetchWeather(false), 60000); }
        } else if (!retryTimerId) { retryTimerId = setInterval(() => fetchWeather(false), 5000); }
      }
    };
    const prev = prevWeatherLocationRef.current;
    const shouldForceRefresh = prev !== undefined && prev !== weatherLocation;
    prevWeatherLocationRef.current = weatherLocation;
    fetchWeather(shouldForceRefresh);
    return () => { cancelled = true; if (weatherTimerId) clearInterval(weatherTimerId); if (retryTimerId) clearInterval(retryTimerId); };
  }, [api, weatherLocation, settingsLoading, refreshSettings, weatherRetryTrigger]);

  const handleOpenWeatherForecast = useCallback((anchorRect) => {
    if (!weatherLocation || weatherTemp === '--' || weatherDesc === 'Weather unavailable') {
      return;
    }
    setWeatherForecastAnchor(anchorRect ? {
      left: anchorRect.left,
      top: anchorRect.top,
      right: anchorRect.right,
      bottom: anchorRect.bottom,
      width: anchorRect.width,
      height: anchorRect.height,
    } : null);
    setWeatherForecastError('');
    setWeatherForecastOpen(true);
  }, [weatherDesc, weatherLocation, weatherTemp]);

  const handleCloseWeatherForecast = useCallback(() => {
    setWeatherForecastOpen(false);
  }, []);

  useEffect(() => {
    if (!weatherForecastOpen || !weatherLocation) return undefined;
    const now = Date.now();
    if (
      hasForecastPayload(weatherForecastData)
      && now - weatherForecastFetchedAt < WEATHER_FORECAST_TTL_MS
    ) {
      return undefined;
    }
    if (hasForecastPayload(weatherData)) {
      setWeatherForecastData(weatherData);
      setWeatherForecastFetchedAt(now);
      return undefined;
    }

    let cancelled = false;
    setWeatherForecastLoading(true);
    setWeatherForecastError('');
    api.getWeather(weatherLocation)
      .then((data) => {
        if (cancelled) return;
        setWeatherData(data);
        setWeatherForecastData(data);
        setWeatherForecastFetchedAt(Date.now());
      })
      .catch(() => {
        if (!cancelled) setWeatherForecastError("Forecast isn't available right now.");
      })
      .finally(() => {
        if (!cancelled) setWeatherForecastLoading(false);
      });

    return () => { cancelled = true; };
  }, [
    api,
    weatherData,
    weatherForecastData,
    weatherForecastFetchedAt,
    weatherForecastOpen,
    weatherLocation,
  ]);

  // Close menu on outside click
  useEffect(() => {
    const handleClick = (e) => { if (menuOpen && !e.target.closest('.menu-container')) { setMenuOpen(false); } };
    document.addEventListener('click', handleClick);
    return () => document.removeEventListener('click', handleClick);
  }, [menuOpen]);

  useEffect(() => {
    const handleCommandPaletteShortcut = (event) => {
      if (!isCommandPaletteShortcut(event)) return;
      if (shouldIgnoreCommandPaletteShortcut(event, {
        commandPaletteOpen,
        modalOpen: settingsOpen || queueOpen || historyOpen || roomsOpen || bugReportOpen || workbenchPanelOpen,
      })) {
        return;
      }
      event.preventDefault();
      setCommandPaletteOpen(true);
    };
    document.addEventListener('keydown', handleCommandPaletteShortcut);
    return () => document.removeEventListener('keydown', handleCommandPaletteShortcut);
  }, [bugReportOpen, commandPaletteOpen, historyOpen, queueOpen, roomsOpen, settingsOpen, workbenchPanelOpen]);

  // Keyboard input handling
  const pttHotkeyHeldRef = React.useRef('');
  const pttHotkey = userSettings?.ptt_hotkey || DEFAULT_PTT_HOTKEY;
  const muteHotkey = userSettings?.mute_hotkey || DEFAULT_MUTE_HOTKEY;
  const micMuted = Boolean(userSettings?.mic_muted);
  const toggleMicMuted = useCallback(() => {
    const nextMuted = !micMuted;
    updateSetting('mic_muted', nextMuted);
    showNotification(
      'Viola',
      { body: nextMuted ? 'Microphone muted' : 'Microphone unmuted' },
      userSettings?.show_notifications ?? true,
    );
  }, [micMuted, updateSetting, userSettings?.show_notifications]);
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (settingsOpen || queueOpen || historyOpen || roomsOpen || bugReportOpen || workbenchPanelOpen || commandPaletteOpen) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (isHotkeyEvent(e, muteHotkey) && !isTyping && !e.repeat) {
        e.preventDefault();
        toggleMicMuted();
        return;
      }
      if (isHotkeyEvent(e, pttHotkey) && !isTyping && !e.repeat) {
        e.preventDefault();
        if (!pttHotkeyHeldRef.current) { pttHotkeyHeldRef.current = e.code; handlePTTStart(); }
        return;
      }
      if (e.key === 'Enter' && isTyping && typingInput.trim()) {
        e.preventDefault();
        const text = typingInput.trim();
        setTypingInput(''); setIsTyping(false);
        // Type-anywhere is its own turn entry point (no composer involved), so
        // it takes the same first-run gate as every other one. Without this a
        // brand-new cloud user who just started typing on the stage got a dead
        // streaming turn and no prompt.
        if (interceptCloudConsent({ kind: 'text', text })) return;
        setChatHistory(prev => [...prev, { role: 'user', content: text, timestamp: new Date().toISOString() }]);
        streamingResponseRef.current = '';
        setLastResponse('"Processing..."'); setIsCommandLoading(true);
        setMainThinking('');
        api.sendCommandStreaming(text, (token) => {
          streamingResponseRef.current += token;
          setLastResponse(`"${streamingResponseRef.current}"`);
        }, [], (thinking) => {
          setMainThinking(prev => `${prev}${thinking}`);
        }).then(result => {
          if (result) handleCommandResult(result);
        }).catch(() => { setLastResponse('"Sorry, something went wrong."'); }).finally(() => { setIsCommandLoading(false); });
        return;
      }
      if (e.key === 'Escape' && isTyping) { setTypingInput(''); setIsTyping(false); return; }
      if (e.key === 'Backspace' && isTyping) {
        e.preventDefault();
        setTypingInput(prev => { const newVal = prev.slice(0, -1); if (newVal.length === 0) setIsTyping(false); return newVal; });
        return;
      }
      if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault(); setIsTyping(true); setTypingInput(prev => prev + e.key);
        return;
      }
    };
    const handleKeyUp = (e) => {
      if (e.code === pttHotkeyHeldRef.current) {
        e.preventDefault();
        pttHotkeyHeldRef.current = '';
        handlePTTEnd();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('keyup', handleKeyUp);
    return () => { document.removeEventListener('keydown', handleKeyDown); document.removeEventListener('keyup', handleKeyUp); };
  }, [isTyping, typingInput, settingsOpen, queueOpen, historyOpen, roomsOpen, bugReportOpen, workbenchPanelOpen, commandPaletteOpen, pttHotkey, muteHotkey, toggleMicMuted, api, handlePTTStart, handlePTTEnd, handleCommandResult, interceptCloudConsent]);

  const formatTime = (date) => formatTimeDisplay(date, userSettings.time_display_format || 'auto');
  const formatDate = (date) => date.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
  const activeStagePillMode = getActiveStagePillMode(displayMode);
  const stageContentMode = resolveStageContentMode(displayMode, {
    activeCallId,
    browserModeActive,
    agentFrameSrc,
    browserUrl,
  });
  const isChatStageActive = stageContentMode === 'chat';

  // BottomRow is the ONLY place in the app that renders `voice.error`, and it
  // is not mounted while the chat stage is up (see the `!isChatStageActive`
  // guard on its render below). The chat composer still has a mic button, so a
  // voice turn can be started from a stage that cannot show it failing: the
  // press was simply indistinguishable from a dead button. The toast is
  // stage-independent, so route the failure there while the response area is
  // absent. Only while it is absent — otherwise the same sentence would be
  // said twice.
  const lastToastedVoiceErrorRef = useRef(null);
  useEffect(() => {
    if (!isChatStageActive) {
      lastToastedVoiceErrorRef.current = null;
      return;
    }
    // Guard on the raw value, not on the described text: describeError is
    // total (it answers a generic sentence for null), so testing its output
    // would raise a toast on every mount with nothing wrong.
    if (!voice.error) {
      lastToastedVoiceErrorRef.current = null;
      return;
    }
    const message = describeError(voice.error);
    if (message === lastToastedVoiceErrorRef.current) return;
    lastToastedVoiceErrorRef.current = message;
    addToast({ message, level: 'error' });
  }, [isChatStageActive, voice.error, addToast]);

  const buildBugReportContext = useCallback((includeScreenContext) => {
    const browserLocation = typeof window !== 'undefined' ? window.location.href : '';
    const viewport = typeof window !== 'undefined'
      ? {
        width: window.innerWidth,
        height: window.innerHeight,
        device_pixel_ratio: window.devicePixelRatio || 1,
      }
      : {};
    const recentAction = agentTaskActive
      ? {
        kind: 'agent_task',
        phase: agentTaskPhase || null,
        status: agentTaskStatus || null,
        description: agentTaskDescription || null,
      }
      : activeCallId
        ? {
          kind: 'phone_call',
          call_id: activeCallId,
        }
        : stageContentMode === 'browser'
          ? {
            kind: 'browser_stage',
            url: browserUrl || null,
          }
          : nowPlaying
            ? {
              kind: 'playback',
              provider: nowPlaying.provider || null,
              title: nowPlaying.title || null,
              is_playing: Boolean(playerState.is_playing),
            }
            : {
              kind: 'ui_stage',
              display_mode: displayMode,
              stage_mode: stageContentMode,
            };
    return {
      source: 'react_topbar',
      surface: isSpoke ? 'react_spoke' : isWebClient() ? 'react_web' : 'react_desktop_shell',
      ui_entrypoint: 'react_topbar',
      current_url: browserLocation,
      display_mode: displayMode,
      stage_mode: stageContentMode,
      recent_action: recentAction,
      viewport,
      player: {
        connected: Boolean(playerState.connected),
        is_playing: Boolean(playerState.is_playing),
        provider: nowPlaying?.provider || null,
        title: nowPlaying?.title || null,
        artist: nowPlaying?.artist || null,
      },
      agent_task: {
        active: agentTaskActive,
        description: agentTaskDescription,
        status: agentTaskStatus,
        phase: agentTaskPhase,
      },
      phone: {
        active_call_id: activeCallId,
      },
      screen_capture_metadata: includeScreenContext
        ? {
          requested: true,
          provided: false,
          storage: 'metadata_only',
          reason: 'browser_capture_unavailable',
        }
        : {
          requested: false,
          provided: false,
          storage: 'metadata_only',
        },
    };
  }, [
    activeCallId,
    agentTaskActive,
    agentTaskDescription,
    agentTaskPhase,
    agentTaskStatus,
    browserUrl,
    displayMode,
    isSpoke,
    nowPlaying,
    nowPlaying?.artist,
    nowPlaying?.provider,
    nowPlaying?.title,
    playerState.connected,
    playerState.is_playing,
    stageContentMode,
  ]);
  const handleBugReportSubmit = useCallback(async ({
    message,
    includeScreenContext,
    contact = '',
    steps = '',
    expected = '',
    actual = '',
    appVersion = '',
    os = '',
  }) => {
    // Merge the user-consented, form-visible fields onto the auto-built UI
    // context. These are the closed set the backend's WebBugReportContext
    // allowlists (surface/app_version/os/contact/steps/expected/actual). Every
    // value here was either typed by the user or shown to them verbatim in the
    // modal's "Sending: Viola v… on …" line before Send -- nothing is populated
    // from anything they could not see.
    const context = {
      ...buildBugReportContext(includeScreenContext),
      app_version: appVersion,
      os,
      contact,
      steps,
      expected,
      actual,
    };
    const result = await api.submitBugReport(message, context);
    setBugReportOpen(false);
    const ticketSuffix = result?.bug_ticket_id ? ` Ticket #${result.bug_ticket_id}.` : '';
    addToast({ message: `Bug report sent.${ticketSuffix}`, level: 'info' });
    return result;
  }, [addToast, api, buildBugReportContext]);

  const handleOpenBugReport = useCallback(async () => {
    setMenuOpen(false);
    const opened = await openSentryUserFeedback(buildBugReportContext(false));
    if (!opened) {
      setBugReportOpen(true);
    }
  }, [buildBugReportContext]);

  // Music leads the pill bar (founder ruling 2026-07-14, #1528): Viola's home
  // surface is the music/now-playing tab (displayMode defaults to
  // 'now_playing', see project_viola_ambient_device_framing.md), an ambient
  // display like an Echo Show, not the chat screen -- so the tab order must
  // not contradict that by pinning Chat leftmost. Phone keeps second (a core,
  // frequently-live capability with its own status dot); Chat moves last, in
  // line with the founder calling it "secondary plumbing" relative to music.
  const pinnedStageItems = [
    { id: 'music', label: 'Music', icon: 'music', ariaLabel: 'Open music mode' },
    {
      id: 'phone',
      label: 'Phone',
      icon: 'phone',
      ariaLabel: activeCallId ? 'Open phone mode, active call in progress' : 'Open phone mode',
      statusDot: Boolean(activeCallId),
      meta: activeCallId ? 'Live' : undefined,
      pulse: Boolean(activeCallId),
    },
    { id: 'chat', label: 'Chat', icon: 'chat', ariaLabel: 'Open chat mode' },
  ];
  const contextualStageItems = agentContextPillVisible ? [{
    id: 'agent',
    label: getAgentPillLabel(agentTaskStatus, agentTaskPhase, agentTaskActive),
    icon: 'globe',
    ariaLabel: 'Open agent browser mode',
    statusDot: agentTaskActive,
    pulse: agentTaskActive,
    activeAliases: ['browser'],
  }] : [];
  const coreStageCommands = useMemo(() => createCoreStageCommands({
    openMode: handleStageModeSelect,
    openSettings: () => {
      setSettingsInitialTab(null);
      setSettingsInitialSection(null);
      setSettingsOpen(true);
    },
  }), [handleStageModeSelect]);
  const {
    commands: commandPaletteCommands,
    registerCommands: registerStageCommands,
  } = useCommandRegistry(coreStageCommands);
  const stageCommandRegistry = useMemo(() => ({
    registerCommands: registerStageCommands,
  }), [registerStageCommands]);
  const chatStageCommands = useMemo(() => createChatStageCommands({
    openMode: handleStageModeSelect,
    dispatchNewChat: () => {
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('viola:chat:new'));
      }
    },
  }), [handleStageModeSelect]);
  const browserStageCommands = useMemo(() => createBrowserStageCommands({
    openMode: handleStageModeSelect,
  }), [handleStageModeSelect]);
  useEffect(() => registerStageCommands('stage.mode.chat', chatStageCommands), [chatStageCommands, registerStageCommands]);
  useEffect(() => registerStageCommands('stage.mode.browser', browserStageCommands), [browserStageCommands, registerStageCommands]);
  const musicStageView = (
    <PlayerSection
      nowPlaying={nowPlaying}
      isPlaying={isPlaying}
      isSpoke={isSpoke}
      browserOverlayVisible={browserOverlayVisible}
      displayProgress={displayProgress}
      displayPosition={displayPosition}
      displayDuration={displayDuration}
      volume={volume}
      shuffleOn={shuffleOn}
      repeatMode={repeatMode}
      rating={rating}
      canResumePlayback={canResumePlayback}
      onPlayPause={handlePlayPause}
      onNext={handleNext}
      onPrevious={handlePrevious}
      onSeek={(seekPos) => {
        api.seek(seekPos).catch((err) => notifyTransportFailure(err, {
          refused: "Can't seek in this track right now.",
          failed: "Couldn't seek. Please try again.",
        }));
        sendToIframe('seekTo', { seconds: seekPos });
      }}
      onVolumeChange={(newVol) => {
        api.setVolume(newVol).catch((err) => notifyTransportFailure(err, {
          refused: "Can't change the volume right now.",
          failed: "Couldn't change the volume. Please try again.",
        }));
        sendToIframe('setVolume', { level: newVol });
      }}
      onShuffleToggle={handleShuffleToggle}
      onRepeatCycle={() => {
        const modes = ['off', 'all', 'one'];
        const idx = modes.indexOf(repeatMode);
        const next = modes[(idx + 1) % modes.length];
        setRepeatMode(next);
        // Optimistic like shuffle, so it rolls back to what the server still
        // holds when the write fails instead of showing a mode nobody stored.
        api.setRepeat(next).catch((err) => {
          setRepeatMode(repeatMode);
          notifyTransportFailure(err, {
            refused: "Can't change repeat right now.",
            failed: "Couldn't change repeat. Please try again.",
          });
        });
      }}
      onRating={handleRating}
      onSubmitText={submitTextCommand}
      mediaAreaRef={mediaAreaRef}
      iframeRef={iframeRef}
      YouTubeEmbed={YouTubeEmbed}
      ProviderEmbed={ProviderEmbed}
      playerState={playerState}
    />
  );
  const phoneStageView = activeCallId ? (
    <PhoneCallPanel
      callId={activeCallId}
      callMeta={activeCallPanelMeta}
      onEndCall={handleEndActiveCall}
      isListening={callAudioListening}
      transcripts={callTranscripts}
      takeoverActive={callTakeoverActive}
      takeoverPending={callOwnerTakeoverPending}
      onToggleListen={handleToggleCallListen}
      onToggleTakeover={handleToggleCallTakeover}
      onActivateTakeover={handleActivateCallTakeover}
      onSendOperatorMessage={sendCallOperatorMessage}
      onOpenHistory={handleOpenPhoneHistory}
      activeConsultation={activeInlineConsultation}
      onConsultationReply={handleConsultationReply}
      onConsultationTakeover={handleConsultationTakeover}
      queuedCalls={phoneCallQueue}
      onRemoveQueuedCall={handleRemoveQueuedCall}
      recordingActive={Boolean(
        activeCallPanelMeta.recordingActive
        || activeCallPanelMeta.recording_active
        || activeCallPanelMeta.recording
      )}
    />
  ) : (
    // Keyed on the signed-in principal (same computation as ChatMode's
    // principalKey below, #2395/C-071 sibling): CallHistoryList's own fetch
    // effect only ever fires once for the component's life (useCallback
    // deps never change), so without a key an in-place desktop sign-out/
    // sign-in never remounts it and the new account's phone panel keeps
    // rendering the previous account's call history, transcripts, and
    // recording links.
    <CallHistoryList key={accountUser?.id || 'device'} focusCallId={phoneHistoryFocusCallId} />
  );
  // Cloud/LAN web client: render frames from the dedicated /ws/agent-browser
  // stream and relay agent_browser_input over it (control actions stay on
  // wsSend / the shared /ws/events socket inside BrowserMode). Spokes and the
  // desktop hub keep the existing /ws/events frame + input path (inputSend
  // null -> BrowserMode falls back to wsSend).
  const browserFrameSrc = useDedicatedBrowserStream ? dedicatedFrameSrc : agentFrameSrc;
  const browserFrameFade = useDedicatedBrowserStream ? dedicatedFrameFade : agentFrameFade;
  const browserInputSend = useDedicatedBrowserStream ? agentBrowserSendInput : null;
  const browserStreamStatus = useDedicatedBrowserStream ? agentBrowserStreamStatus : null;
  const browserStreamError = useDedicatedBrowserStream ? agentBrowserStreamError : null;
  const browserStageView = (
    <BrowserMode
      browserUrl={browserUrl}
      agentTask={{
        description: agentTaskDescription,
        phase: agentTaskPhase,
        status: agentTaskStatus,
      }}
      agentBusy={agentTaskActive}
      recentlyActive={agentContextPillVisible && !agentTaskActive}
      takeoverActive={agentTakeoverActive}
      isSpoke={isSpoke}
      streamFrames={streamBrowserView}
      agentFrameSrc={browserFrameSrc}
      agentFrameFade={browserFrameFade}
      wsSend={wsSend}
      inputSend={browserInputSend}
      streamStatus={browserStreamStatus}
      streamError={browserStreamError}
      onTakeoverChange={setAgentTakeoverActive}
      onExit={activateMusicMode}
      onPTTStart={handlePTTStart}
      onPTTEnd={handlePTTEnd}
      commandRegistry={stageCommandRegistry}
      commandScopeActive={stageContentMode === 'browser'}
    />
  );
  const stageModeRenderers = {
    music: musicStageView,
    phone: phoneStageView,
    chat: (
      <ChatMode
        handlePTTStart={handlePTTStart}
        handlePTTEnd={handlePTTEnd}
        onOpenSettings={() => {
          setSettingsInitialTab(null);
          setSettingsInitialSection(null);
          setSettingsOpen(true);
        }}
        profileName={accountUser?.user_metadata?.full_name || accountUser?.user_metadata?.name || accountUser?.email || userSettings?.display_name || userSettings?.name || 'Local user'}
        commandRegistry={stageCommandRegistry}
        commandScopeActive={stageContentMode === 'chat'}
        principalKey={accountUser?.id || 'device'}
      />
    ),
    browser: browserStageView,
  };
  const stageOverlays = null;
  const renderedStageMode = stageContentMode;

  // =========================================================================
  // RENDER
  // =========================================================================
  return (
    <CloudConsentGateProvider intercept={interceptCloudConsent}>
    <div className="viola-outer-container smart-display" data-testid="smart-display" style={{
      width: '100%',
      height: isSpoke ? '100%' : '100dvh',
      minHeight: '520px',
      backgroundColor: THEME.colors.bgVoid,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '10px',
      boxSizing: 'border-box',
      touchAction: 'manipulation',
    }}>
      {/* Loading skeleton */}
      {!playerState.connected && (
        <LoadingSkeleton
          title={isSpoke ? 'Connecting speaker' : undefined}
          subtitle={isSpoke ? 'Connecting to hub...' : undefined}
          detail={isSpoke && spokeBackendTimedOut
            ? 'Still waiting for the hub. Reopen Add Room and scan the newest QR code, or check that both devices are on the same Wi-Fi.'
            : ''}
        />
      )}

      {/* Inner card — the background content. When any modal is open, it sits
          visually behind the modal's z-index:1000 backdrop, so it is hidden
          from assistive tech and the display-integrity scanner (aria-hidden)
          and made non-interactive/non-focusable (inert). The modals below
          are rendered as siblings of this wrapper, not inside it, so hiding
          this wrapper never hides the modals themselves. */}
      <div
        className="viola-inner-card"
        aria-hidden={anyModalOpen ? 'true' : undefined}
        inert={anyModalOpen ? '' : undefined}
        style={{
          width: '100%',
          maxWidth: '1400px',
          height: '100%',
          maxHeight: '800px',
          minHeight: '500px',
          backgroundColor: THEME.colors.bgCard,
          borderRadius: '32px',
          padding: 'clamp(24px, 3.4vw, 42px) clamp(36px, 5.5vw, 72px)',
          boxSizing: 'border-box',
          display: 'flex',
          flexDirection: 'column',
          fontFamily: THEME.fonts.sans,
          color: THEME.colors.textPrimary,
          position: 'relative',
          boxShadow: `inset 0 0 0 1px ${THEME.colors.borderSubtle}`,
        }}>
        {/* Top Bar */}
        <TopBar
          currentTime={currentTime}
          formatTime={formatTime}
          formatDate={formatDate}
          weatherLocation={weatherLocation}
          weatherCondition={weatherCondition}
          weatherTemp={weatherTemp}
          weatherDesc={weatherDesc}
          weatherRetryTrigger={weatherRetryTrigger}
          setWeatherRetryTrigger={setWeatherRetryTrigger}
          setWeatherDesc={setWeatherDesc}
          menuOpen={menuOpen}
          setMenuOpen={setMenuOpen}
          onOpenHistory={() => { setMenuOpen(false); setHistoryOpen(true); }}
          onOpenQueue={() => { setMenuOpen(false); setQueueOpen(true); }}
          onOpenRooms={() => { setMenuOpen(false); setRoomsInitialTab('add-speaker'); setRoomsPrefill(null); setRoomsOpen(true); }}
          onOpenSettings={() => { setMenuOpen(false); setSettingsInitialTab(null); setSettingsInitialSection(null); setSettingsOpen(true); }}
          onOpenHelp={() => { setMenuOpen(false); setHelpOpen(true); }}
          onOpenWeatherSettings={() => { setSettingsInitialTab('customize'); setSettingsInitialSection(null); setSettingsOpen(true); }}
          onOpenWeatherForecast={handleOpenWeatherForecast}
          onOpenBugReport={handleOpenBugReport}
          onCalendarModalOpenChange={setCalendarModalOpen}
          userSettings={userSettings}
          activeAgentCount={activeAgentCount}
          agentDrawerExpanded={agentDrawerExpanded}
          onToggleAgentDrawer={handleToggleAgentDrawer}
        />

        <div
          // When the nav dropdown menu is open it renders as an overlay
          // directly over this pill row; hide the row from assistive tech and
          // the display-integrity scanner (and make it non-interactive) so the
          // menu items don't register as overlapping the covered pills.
          aria-hidden={menuOpen ? 'true' : undefined}
          inert={menuOpen ? '' : undefined}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'flex-start',
            gap: '12px',
            minHeight: '44px',
            marginTop: '4px',
            marginBottom: '6px',
            zIndex: 55,
          }}
        >
          <PillBar
            activeMode={activeStagePillMode}
            pinnedItems={pinnedStageItems}
            contextualItems={contextualStageItems}
            onSelect={handleStageModeSelect}
            iconOnlyPinned={true}
            trailing={(
              <button
                type="button"
                aria-label="Open memory and Workbench"
                onClick={() => setWorkbenchPanelOpen(true)}
                title="Open memory and Workbench"
                data-testid="stage-workbench-plus"
                className="stage-workbench-plus"
                style={{
                  width: '44px',
                  height: '44px',
                  padding: 0,
                  borderRadius: '999px',
                  display: 'inline-grid',
                  placeItems: 'center',
                  border: `1px solid ${THEME.colors.borderHover}`,
                  background: 'transparent',
                  color: THEME.colors.textSecondary,
                  boxShadow: `0 8px 24px ${THEME.colors.shadowLight}`,
                  cursor: 'pointer',
                  marginLeft: '4px',
                }}
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <line x1="12" y1="5" x2="12" y2="19" />
                  <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
              </button>
            )}
          />
        </div>

        <SubtleDivider />

        {/* Stage content behind the TopBar's expanded-calendar modal. That
            modal (CalendarView.jsx, compact mode) renders nested INSIDE
            TopBar, which is itself inside viola-inner-card -- unlike every
            other modal in the app (rendered as a sibling below, outside this
            wrapper), so it cannot use the top-level anyModalOpen/inert on
            viola-inner-card without making itself inert too. Scope
            aria-hidden/inert to just this stage block instead: it hides the
            same background content (chat/music/phone panels, bottom row)
            the calendar modal's backdrop visually covers, without touching
            TopBar or the modal nested inside it. #2568. */}
        <div
          aria-hidden={calendarModalOpen ? 'true' : undefined}
          inert={calendarModalOpen ? '' : undefined}
          style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}
        >
        {lastEndedCall && (
          <div
            style={{
              position: 'absolute',
              top: '86px',
              left: '50%',
              transform: 'translateX(-50%)',
              zIndex: 68,
              pointerEvents: 'auto',
            }}
          >
            <CallSummaryCard
              call={lastEndedCall}
              onDismiss={() => setLastEndedCall(null)}
              onViewTranscript={handleViewEndedTranscript}
            />
          </div>
        )}

        <Stage
          ref={stageRef}
          mode={renderedStageMode}
          modeRenderers={stageModeRenderers}
          overlays={stageOverlays}
          onStageRectChange={handleStageRectChange}
        />

        {/* Hands-free hot-mic disclosure: rendered UNCONDITIONALLY (outside
            the chat-stage toggle) — while the wake mic is active the hot-mic
            state must be visible on every stage, never hidden by layout
            state. Renders nothing when hands-free is off. */}
        <HandsFreeMicIndicator status={browserWake.status} error={browserWake.error} />

        {!isChatStageActive && (
          <>
            <SubtleDivider />

            {/* Cloud->desktop auto-link visibility: shows when the last turn
                ran on the user's own desktop. Renders nothing otherwise. */}
            {relayDeviceName && (
              <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '8px' }}>
                <DesktopRelayIndicator deviceName={relayDeviceName} />
              </div>
            )}

            {/* Bottom Row */}
            <BottomRow
              isTyping={isTyping}
              typingInput={typingInput}
              setTypingInput={setTypingInput}
              isCommandLoading={isCommandLoading}
              lastResponse={lastResponse}
              mainThinking={mainThinking}
              voice={voice}
              processingTooLong={processingTooLong}
              capDenial={capDenial}
              onUpgradeFromCap={handleUpgradeFromCap}
              wakeStatus={wakeStatus}
              handlePTTStart={handlePTTStart}
              handlePTTEnd={handlePTTEnd}
              handleMicIntent={handleMicIntent}
              connected={playerState.connected}
            />
          </>
        )}

        {/* Provider Badge */}
        {displayMode === 'now_playing' && playerState.now_playing?.provider === 'browser' && (
          <ProviderBadge />
        )}

        {weatherForecastOpen && (
          <WeatherForecast
            forecastData={weatherForecastData || weatherData}
            unitsPreference={userSettings?.weather_units || 'imperial'}
            theme={THEME}
            anchorRect={weatherForecastAnchor}
            loading={weatherForecastLoading}
            error={weatherForecastError}
            onClose={handleCloseWeatherForecast}
          />
        )}

        {/* Calendar Mode Overlay */}
        {displayMode === 'calendar' && (
          <div
            data-testid="calendar-view-panel"
            className="display-mode-overlay entering"
            style={{
              position: 'absolute',
              inset: '20px',
              zIndex: 42,
              backgroundColor: THEME.colors.bgCard,
              border: `1px solid ${THEME.colors.borderSubtle}`,
              borderRadius: '24px',
              padding: '20px',
              boxShadow: '0 24px 80px rgba(0, 0, 0, 0.35)',
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '12px', flexShrink: 0 }}>
              <button
                type="button"
                aria-label="Close calendar"
                onClick={() => {
                  setCalendarModeActive(false);
                  setDisplayMode('now_playing');
                }}
                style={{
                  width: '36px',
                  height: '36px',
                  borderRadius: '8px',
                  border: `1px solid ${THEME.colors.borderSubtle}`,
                  backgroundColor: THEME.colors.bgElevated,
                  color: THEME.colors.textPrimary,
                  cursor: 'pointer',
                  fontSize: '18px',
                  lineHeight: 1,
                }}
              >
                X
              </button>
            </div>
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
              <CalendarView
                theme={THEME}
                timeFormat={userSettings.time_display_format || 'auto'}
                compact={false}
              />
            </div>
          </div>
        )}
        </div>
      </div>

      <AgentDrawer
        active={agentRegistry.active}
        recentCompleted={agentRegistry.recent_completed}
        streamStates={agentRegistry.streamStates}
        expanded={agentDrawerExpanded}
        onToggleExpanded={handleToggleAgentDrawer}
        onCancel={agentRegistry.cancelAgent}
      />

      <WorkbenchDropZone addToast={addToast} />

      <CommandPalette
        open={commandPaletteOpen}
        commands={commandPaletteCommands}
        onClose={() => setCommandPaletteOpen(false)}
      />

      {/* Modals */}
      <BugReportModal
        isOpen={bugReportOpen}
        onClose={() => setBugReportOpen(false)}
        onSubmit={handleBugReportSubmit}
      />
      {settingsOpen && (
        <ChunkLoadErrorBoundary name="Settings">
          <Suspense fallback={<ModalLoadingSpinner />}>
            <SettingsModal
              isOpen={settingsOpen}
              onClose={handleSettingsClose}
              initialTab={settingsInitialTab}
              initialSection={settingsInitialSection}
            />
          </Suspense>
        </ChunkLoadErrorBoundary>
      )}
      {queueOpen && (
        <ChunkLoadErrorBoundary name="Queue">
          <Suspense fallback={<ModalLoadingSpinner />}>
            <QueueModal isOpen={queueOpen} onClose={() => setQueueOpen(false)} wsQueue={playerState.queue} />
          </Suspense>
        </ChunkLoadErrorBoundary>
      )}
      {historyOpen && (
        <ChunkLoadErrorBoundary name="History">
          <Suspense fallback={<ModalLoadingSpinner />}>
            <HistoryModal isOpen={historyOpen} onClose={() => setHistoryOpen(false)} history={chatHistory} onClearHistory={() => setChatHistory([])} timeFormat={userSettings.time_display_format || 'auto'} />
          </Suspense>
        </ChunkLoadErrorBoundary>
      )}
      {roomsOpen && (
        <ChunkLoadErrorBoundary name="Rooms">
          <Suspense fallback={<ModalLoadingSpinner />}>
            <RoomGroupsModal
              isOpen={roomsOpen}
              onClose={() => {
                setRoomsOpen(false);
                setRoomsInitialTab('add-speaker');
                setRoomsPrefill(null);
              }}
              availableRooms={availableRooms}
              initialTab={roomsInitialTab}
              prefill={roomsPrefill}
            />
          </Suspense>
        </ChunkLoadErrorBoundary>
      )}

      {/* Help & Guide modal (rich: Commands / Troubleshooting / About tabs) */}
      {helpOpen && (
        <ChunkLoadErrorBoundary name="Help">
          <Suspense fallback={<ModalLoadingSpinner />}>
            <HelpModal isOpen={helpOpen} onClose={() => setHelpOpen(false)} />
          </Suspense>
        </ChunkLoadErrorBoundary>
      )}

      {workbenchPanelOpen && (
        <MemoryPanel
          isOpen={workbenchPanelOpen}
          onClose={() => setWorkbenchPanelOpen(false)}
        />
      )}

      {/* Onboarding overlays */}
      <OnboardingHighlight targetId={onboarding.activeHighlight} active={onboarding.isOnboarding && !!onboarding.activeHighlight} />
      <OnboardingOverlay
        isOnboarding={onboarding.isOnboarding}
        phase={onboarding.phase}
        phaseIndex={onboarding.phaseIndex}
        totalPhases={onboarding.totalPhases}
        phaseContent={onboarding.phaseContent}
        isSpeaking={onboarding.isSpeaking}
        isWelcomePhase={onboarding.isWelcomePhase}
        onWelcomeContinue={onboarding.onWelcomeContinue}
        onReportBug={handleOpenBugReport}
        isAccountPairPhase={onboarding.isAccountPairPhase}
        signInStatus={onboarding.signInStatus}
        accountSkipped={onboarding.accountSkipped}
        onContinueWithoutAccount={onboarding.onContinueWithoutAccount}
        onReturnToSignIn={onboarding.onReturnToSignIn}
        isMicPermissionPhase={onboarding.isMicPermissionPhase}
        micPermission={onboarding.micPermission}
        onMicPermissionRetry={onboarding.onMicPermissionRetry}
        onSkipMicStep={onboarding.onSkipMicStep}
        isCloudConsentPhase={onboarding.isCloudConsentPhase}
        cloudConsentStatus={onboarding.cloudConsentStatus}
        cloudConsentError={onboarding.cloudConsentError}
        onCloudConsentChoice={onboarding.onCloudConsentChoice}
        onByokSetupDone={onboarding.onByokSetupDone}
        isAutonomyPhase={onboarding.isAutonomyPhase}
        autonomyStatus={onboarding.autonomyStatus}
        autonomyError={onboarding.autonomyError}
        onAutonomyChoice={onboarding.onAutonomyChoice}
        isAttributionPhase={onboarding.isAttributionPhase}
        attributionStatus={onboarding.attributionStatus}
        onAttributionChoice={onboarding.onAttributionChoice}
        tryCommandStatus={onboarding.tryCommandStatus}
        suggestions={onboarding.suggestions}
        skipOnboarding={onboarding.skipOnboarding}
        onSuggestionTap={onboarding.onSuggestionTap}
        sendCommand={submitTextCommand}
        onOpenSettings={(tab) => { setSettingsInitialSection(null); setSettingsOpen(true); setSettingsInitialTab(tab); }}
      />

      {activeCard && <ContentCard data={activeCard} onDismiss={dismissCard} />}

      {/* Phone-call UX toasts (bottom-right) driven by telephony WS events */}
      <CallBriefing briefing={callBriefing} onDismiss={() => setCallBriefing(null)} />
      {!activeInlineConsultation && (
        <CallConsultation
          consultation={callConsultation}
          onReply={handleConsultationReply}
          onTakeover={handleConsultationTakeover}
          onDismiss={() => setCallConsultation(null)}
        />
      )}

      <LoginPromptModal
        isOpen={!!loginPromptPayload}
        payload={loginPromptPayload}
        onClose={() => setLoginPromptPayload(null)}
        onSignIn={() => openSettingsPanel('account')}
      />
      <PhoneToSModal
        isOpen={!!phoneTosPayload}
        payload={phoneTosPayload}
        onClose={() => setPhoneTosPayload(null)}
        onAccepted={() => addToast({ message: 'Phone terms accepted.', level: 'success' })}
      />
      <CloudLlmConsentModal
        isOpen={cloudLlmConsentOpen}
        saving={cloudLlmConsent.saving}
        error={cloudLlmConsent.error}
        onConsent={handleCloudLlmConsentAccept}
        onClose={() => {
          // "Not now" abandons the turn: nothing is resumed if they consent later
          // from a different action.
          pendingConsentActionRef.current = null;
          setCloudLlmConsentOpen(false);
        }}
      />
      <CloudWelcome
        isOpen={cloudWelcomeOpen}
        saving={cloudWelcome.saving}
        onFinish={handleCloudWelcomeFinish}
        onSkip={handleCloudWelcomeSkip}
      />

      <ToastContainer toasts={toasts} onDismiss={removeToast} />

      {/* #1404: live countdown while a timer runs + on-screen expiry toast */}
      <TimerCountdown addToast={addToast} />
    </div>
    </CloudConsentGateProvider>
  );
}

SmartDisplay.propTypes = {
  isSpoke: PropTypes.bool,
  micStream: PropTypes.object,
  room: PropTypes.string,
};
