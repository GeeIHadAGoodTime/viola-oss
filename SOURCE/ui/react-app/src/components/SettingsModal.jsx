import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import PropTypes from 'prop-types';
import { useSettings } from '../hooks/useSettings';
import { useOptionalAuth } from '../hooks/useAuth';
import { useHandsFreeWake } from '../hooks/useHandsFreeWake';
import { isCloudSurface } from './auth/cloudSurface';
import { apiFetch } from '../hooks/useViolaApi';
import { THEME } from '../config';
import { isFeatureHidden } from '../utils/featureSurface';
import DesktopUpsell from './DesktopUpsell';
import AccountTab, { CalendarSettings } from './AccountTab';
import ICloudCalendarSettings from './ICloudCalendarSettings';
import MessagingTab from './MessagingTab';
import WakeWordSection from './WakeWordSection';
import AccentPicker from './AccentPicker';
import CodexAuthCard from './ai/CodexAuthCard';
import AdvancedSettingsWindow from './advanced/AdvancedSettingsWindow';
import { QRPairCard } from './messaging';
import { SmartHomeWizard } from './smarthome';
import { DEFAULT_MUTE_HOTKEY, DEFAULT_PTT_HOTKEY, hotkeyFromKeyboardEvent } from '../utils/hotkeys';
import { getStartupToggleCopy } from '../utils/platform';
import CustomizeTab from './CustomizeTab';

// The version of the build the user is actually running, injected at build
// time from core/constants.py (VIOLA_VERSION) by vite.config.js — the same
// source BugReportModal attaches to reports. The About row used to be the
// literal string "1.0.0", which stayed at 1.0.0 while shipped builds moved on.
const APP_VERSION = import.meta.env.VITE_VIOLA_VERSION || 'Unknown';
// WakeWordSection and AccentPicker also imported by CustomizeTab; kept here
// because SettingsModal-level legacy alias paths may still mount them directly.
// SpokeQRSetup moved to Rooms page (RoomGroupsModal -> ConnectSpeakerPanel)

// Extracted settings components
import {
  Toggle,
  Slider,
  Select,
  SettingRow,
  Section,
  SectionDivider,
  AdvancedSection,
  CloseButton,
  FooterButton,
  InfoRow,
  TtsStatusIndicator,
  ScrollbarStyles,
  Icons,
  useDebouncedCallback,
} from './settings';

// Use centralized theme from config.js
const theme = THEME;
const MODAL_OPEN_DEFER_MS = 50;
const SPOTIFY_LOGIN_POLL_MS = 2000;
const SPOTIFY_LOGIN_TIMEOUT_MS = 120000;

// Searchable settings scopes.
const TABS = [
  { id: 'account', label: 'Account', icon: Icons.Accounts },
  { id: 'ai_agents', label: 'AI & Agents', icon: Icons.AI },
  { id: 'music', label: 'Music', icon: Icons.Music },
  { id: 'voice', label: 'Voice', icon: Icons.Messaging },
  { id: 'connections', label: 'Connections', icon: Icons.Services },
  { id: 'customize', label: 'Customize', icon: Icons.Customize },
  { id: 'system', label: 'System', icon: Icons.Advanced },
];

const LEGACY_TAB_ALIASES = {
  ai: 'ai_agents',
  agents: 'ai_agents',
  account: 'account',
  accounts: 'account',
  profile: 'account',
  payment: 'account',
  payments: 'account',
  payment_methods: 'account',
  music: 'music',
  music_voice: 'music',
  music_accounts: 'music',
  voice: 'voice',
  messaging: 'connections',
  messages: 'connections',
  services: 'connections',
  connected_services: 'connections',
  preferences: 'customize',
  appearance: 'customize',
  weather: 'customize',
  customize: 'customize',
  developer: 'system',
};

const normalizeTabId = (tabId) => {
  if (!tabId) return 'account';
  return LEGACY_TAB_ALIASES[tabId] || tabId;
};

const SECTION_SEARCH_INDEX = {
  account: [
    'account profile plan billing payment methods cards usage upgrade sync devices sign out',
    'phone number recording ai disclosure retention delete call data',
    'privacy data telemetry usage reports sync error reporting session replay wake-word training',
  ],
  ai_agents: [
    'ai source managed byok chatgpt plus codex local model autonomy solo ensemble symphony browser weekly review',
    'agent source reasoning provider browser import session',
  ],
  music: [
    'music source youtube spotify local files sign in playlists playback volume autoplay',
  ],
  voice: [
    'voice wake word sensitivity push-to-talk ptt shortcut text to speech tts voice speed volume stt recognition whisper vad advanced',
  ],
  connections: [
    'connections connected services smart home calendar oauth google telegram qr messaging',
  ],
  customize: [
    'customize theme accent color weather location city zip postal code time format wake word selection',
  ],
  system: [
    'system audio devices microphone speaker multi-room rooms notifications updates early access window tray startup network api port advanced settings',
  ],
};

const matchesSearch = (tab, query) => {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) return true;
  const haystack = [
    tab.label,
    ...(SECTION_SEARCH_INDEX[tab.id] || []),
  ].join(' ').toLowerCase();
  return haystack.includes(normalizedQuery);
};

// ── Tier system constants ──

const TIER_OPTIONS = [
  { id: 'solo', label: 'Solo', subtitle: 'Private & safe' },
  { id: 'ensemble', label: 'Ensemble', subtitle: 'Capable & sandboxed' },
  { id: 'symphony', label: 'Symphony', subtitle: 'Full autonomy' },
];

const TIER_DESCRIPTIONS = {
  solo: 'Viola answers questions, plays music, searches the web, and manages your local calendar. No file access, no email, no browsing, no desktop control. The safest option for shared devices.',
  ensemble: 'Viola can read your files, browse the web in a sandbox, and read email. Anything that modifies data requires your confirmation first.',
  symphony: 'Full access. Viola can write files, send emails, control your desktop, and browse with your saved passwords. Dangerous actions still require confirmation.',
};

const TIER_CAPABILITIES = {
  solo: {
    'Music & playback': true,
    'Web search': true,
    'Memory': true,
    'Calendar': true,
    'File access': false,
    'Email': false,
    'Browser': false,
    'Desktop control': false,
    'Shell commands': false,
    'Scheduling': false,
  },
  ensemble: {
    'Music & playback': true,
    'Web search': true,
    'Memory': true,
    'Calendar': true,
    'File access': true,
    'Email': true,
    'Browser': true,
    'Desktop control': false,
    'Shell commands': false,
    'Scheduling': true,
  },
  symphony: {
    'Music & playback': true,
    'Web search': true,
    'Memory': true,
    'Calendar': true,
    'File access': true,
    'Email': true,
    'Browser': true,
    'Desktop control': true,
    'Shell commands': true,
    'Scheduling': true,
  },
};

// Map tier to browser_session_mode (derived, never shown to user)
const TIER_BROWSER_MODE = {
  solo: 'ephemeral',     // Temp profile, wiped after each session
  ensemble: 'viola',     // Persistent Viola-owned Chromium profile
  symphony: 'viola',     // Persistent Viola-owned Chromium profile
};

function getSafeErrorMessage(message, fallback) {
  if (typeof message !== 'string') return fallback;
  const trimmed = message.trim();
  if (!trimmed) return fallback;

  const technicalPatterns = [
    /^error:/i,
    /traceback/i,
    /exception/i,
    /stack/i,
    /\bCDP\b/i,
    /DevTools/i,
    /QWebEngineView/i,
    /controller_attached/i,
    /logged_in/i,
    /ResponseEnvelope/i,
    /\/v\d+\//i,
    /\bHTTP\s*\d{3}\b/i,
    /\bstatus\s*code\b/i,
  ];

  return technicalPatterns.some((pattern) => pattern.test(trimmed)) ? fallback : trimmed;
}

function normalizeLocalAiServers(rawServers) {
  if (!Array.isArray(rawServers)) return [];
  return rawServers
    .filter((server) => server && typeof server === 'object')
    .map((server) => {
      const models = Array.isArray(server.models)
        ? [...new Set(server.models.filter((model) => typeof model === 'string' && model.trim()).map((model) => model.trim()))]
        : [];
      return {
        type: typeof server.type === 'string' && server.type.trim() ? server.type.trim() : 'ollama',
        name: typeof server.name === 'string' && server.name.trim() ? server.name.trim() : 'Local AI',
        url: typeof server.url === 'string' ? server.url.trim() : '',
        models,
        running: server.running !== false,
        detectedFrom: typeof server.detected_from === 'string' ? server.detected_from : '',
      };
    });
}

function localAiModelValue(server, model) {
  return `${server.type || 'local'}|${server.url || ''}|${model}`;
}

function normalizeConnectorManifests(rawConnectors) {
  if (!Array.isArray(rawConnectors)) return [];
  return rawConnectors
    .filter((connector) => connector && typeof connector === 'object')
    .map((connector) => ({
      id: typeof connector.id === 'string' ? connector.id : '',
      category: typeof connector.category === 'string' ? connector.category : '',
      label: typeof connector.display_name === 'string' ? connector.display_name : connector.id,
      adapter: typeof connector.adapter === 'string' ? connector.adapter : '',
      authType: typeof connector.auth_type === 'string' ? connector.auth_type : '',
      requiresApiKey: connector.requires_api_key === true,
      providerKey: typeof connector.provider_key === 'string' ? connector.provider_key : '',
      defaultBaseUrl: typeof connector.default_base_url === 'string' ? connector.default_base_url : '',
      defaultModels: Array.isArray(connector.default_models) ? connector.default_models.filter((model) => typeof model === 'string') : [],
      tags: Array.isArray(connector.tags) ? connector.tags.filter((tag) => typeof tag === 'string') : [],
      settingHints: connector.setting_hints && typeof connector.setting_hints === 'object' ? connector.setting_hints : {},
      localPresets: Array.isArray(connector.capabilities?.local_presets)
        ? connector.capabilities.local_presets.filter((preset) => (
          preset && typeof preset.name === 'string' && typeof preset.base_url === 'string'
        ))
        : [],
    }))
    .filter((connector) => connector.id && connector.category === 'llm');
}

function normalizeConnectionProfiles(rawProfiles) {
  if (!Array.isArray(rawProfiles)) return [];
  return rawProfiles
    .filter((profile) => profile && typeof profile === 'object')
    .map((profile) => ({
      profileId: typeof profile.profile_id === 'string' ? profile.profile_id : '',
      connectorId: typeof profile.connector_id === 'string' ? profile.connector_id : '',
      category: typeof profile.category === 'string' ? profile.category : '',
      displayName: typeof profile.display_name === 'string' ? profile.display_name : 'Provider profile',
      providerKey: typeof profile.provider_key === 'string' ? profile.provider_key : '',
      adapter: typeof profile.adapter === 'string' ? profile.adapter : '',
      authType: typeof profile.auth_type === 'string' ? profile.auth_type : '',
      baseUrl: typeof profile.base_url === 'string' ? profile.base_url : '',
      model: typeof profile.model === 'string' ? profile.model : '',
      enabled: profile.enabled !== false,
      selected: profile.selected === true,
      secrets: profile.secrets && typeof profile.secrets === 'object' ? profile.secrets : {},  // pragma: allowlist secret
      validation: profile.validation && typeof profile.validation === 'object' ? profile.validation : {},
      updatedAt: typeof profile.updated_at === 'string' ? profile.updated_at : '',
    }))
    .filter((profile) => profile.profileId && profile.connectorId && profile.category === 'llm');
}

function baseUrlsMatch(left, right) {
  return String(left || '').trim().replace(/\/+$/, '').toLowerCase()
    === String(right || '').trim().replace(/\/+$/, '').toLowerCase();
}

function aiSourceForConnector(connector, fallback = 'byok') {
  if (!connector) return fallback;
  if (connector.id === 'llm.managed') return 'managed';
  if (connector.id === 'llm.codex') return 'codex';
  if (connector.tags.includes('local')) return 'local';
  if (connector.tags.includes('byok')) return 'byok';
  return fallback;
}

function validationSummary(profile) {
  const validation = profile?.validation || {};
  if (!Object.keys(validation).length) return 'Not tested yet';
  if (validation.valid === true) {
    const latency = Number.isFinite(Number(validation.latency_ms)) ? `, ${validation.latency_ms}ms` : '';
    const probe = validation.tool_contract?.live_tool_probe;
    const toolText = probe && typeof probe === 'object'
      ? `, tools ${probe.success ? 'ok' : 'failed'}`
      : '';
    return `Valid${latency}${toolText}`;
  }
  return `Failed: ${validation.message || validation.error_code || 'connection test failed'}`;
}

// Main Settings Component
const SettingsModal = React.memo(function SettingsModal({ isOpen, onClose, initialTab, initialSection }) {
  const [activeTab, setActiveTab] = useState(() => normalizeTabId(initialTab));
  const [searchTerm, setSearchTerm] = useState('');
  const [debouncedSearchTerm, setDebouncedSearchTerm] = useState('');
  const [modalWidth, setModalWidth] = useState(() => (typeof window === 'undefined' ? 960 : Math.min(window.innerWidth, 960)));
  const modalRef = useRef(null);

  // Reset to initialTab when modal opens with a specific tab
  useEffect(() => {
    if (isOpen && initialTab) {
      setActiveTab(normalizeTabId(initialTab));
    }
  }, [isOpen, initialTab]);
  const [localSettings, setLocalSettings] = useState({});
  const [hasChanges, setHasChanges] = useState(false);
  // Platform-appropriate label for the login-item / autostart toggle. macOS
  // must not read "Start on Windows Boot" (issue #770); computed from the host
  // OS, stable for the session.
  const startupToggleCopy = useMemo(() => getStartupToggleCopy(), []);
  // Browser hands-free wake word: device-local opt-in (localStorage), applies
  // immediately — it is NOT part of localSettings/save because a hot-mic
  // preference must never sync to other devices.
  const [handsFreeWake, setHandsFreeWake] = useHandsFreeWake();
  // Cloud Sync consent is a property of the ACCOUNT, not of this install: the
  // switch writes the cloud row every Tier-2 gate reads, and turning it off
  // purges the account's synced copy. With nobody signed in there is no row to
  // consent on, so the control is disabled and says why instead of storing a
  // local flag that means nothing (#4789). Optional-auth read: null context
  // reads as signed out, which is the fail-closed direction.
  const cloudAccountAuth = useOptionalAuth();
  const hasCloudAccount = Boolean(cloudAccountAuth?.isLoggedIn);
  const handsFreeSurfaceSupported = useMemo(() => {
    if (isCloudSurface()) return true;
    // Spoke routing (matches main.jsx / App.jsx): ?mode=speaker, ?room=, ?spoke_token=
    const params = new URLSearchParams(window.location.search);
    return params.get('mode') === 'speaker' || params.has('room') || params.has('spoke_token');
  }, []);
  const [paymentCards, setPaymentCards] = useState([]);
  const [paymentCardsLoading, setPaymentCardsLoading] = useState(false);
  const [paymentCardsError, setPaymentCardsError] = useState('');
  const [paymentCardDialog, setPaymentCardDialog] = useState(null);
  const [paymentCardForm, setPaymentCardForm] = useState({
    label: '',
    number: '',
    cvc: '',
    last4: '',
    exp_month: '',
    exp_year: '',
    holder_name: '',
    auth_ceiling_dollars: '',
  });
  const [paymentCardSaving, setPaymentCardSaving] = useState(false);
  const [paymentCardActionError, setPaymentCardActionError] = useState('');
  const [isDetectingLocalAi, setIsDetectingLocalAi] = useState(false);
  const [localAiDetectStatus, setLocalAiDetectStatus] = useState('');
  const [localAiServers, setLocalAiServers] = useState([]);
  const [llmConnectorManifests, setLlmConnectorManifests] = useState([]);
  const [llmConnectorStatus, setLlmConnectorStatus] = useState('');
  const [llmProfiles, setLlmProfiles] = useState([]);
  const [llmProfilesStatus, setLlmProfilesStatus] = useState('');
  const [llmProfileActionStatus, setLlmProfileActionStatus] = useState('');
  const [llmProfileBusyId, setLlmProfileBusyId] = useState('');
  const [isDeletingCallData, setIsDeletingCallData] = useState(false);
  const [callDataDeleteStatus, setCallDataDeleteStatus] = useState('');
  const [showAdvancedSettings, setShowAdvancedSettings] = useState(false);

  const {
    settings,
    loading,
    saving,
    error,
    devices,
    playlists,
    updateSettings,
    syncPlaylists,
    renamePlaylist,
    setDefaultPlaylist,
    deletePlaylist,
    clearError,
    refreshDevices,
    refreshPlaylists,
  } = useSettings({ initialFetchDelayMs: MODAL_OPEN_DEFER_MS });
  const currentAiSource = localSettings.ai_source || 'managed';
  const localAiModelOptions = useMemo(() => localAiServers.flatMap((server) => (
    server.models.map((model) => ({
      value: localAiModelValue(server, model),
      label: `${model} (${server.name}${server.running === false ? ' installed' : ''})`,
      model,
      provider: server.type || 'ollama',
      baseUrl: server.url || '',
    }))
  )), [localAiServers]);
  const selectedLocalAiModelOption = useMemo(() => localAiModelOptions.find((option) => (
    option.model === localSettings.llm_model
    && (!localSettings.llm_base_url || option.baseUrl === localSettings.llm_base_url)
    && (!localSettings.llm_provider || option.provider === localSettings.llm_provider)
  )) || localAiModelOptions.find((option) => option.model === localSettings.llm_model), [
    localAiModelOptions,
    localSettings.llm_base_url,
    localSettings.llm_model,
    localSettings.llm_provider,
  ]);
  const localAiModelSelectOptions = useMemo(() => {
    if (!localSettings.llm_model || selectedLocalAiModelOption) {
      return localAiModelOptions;
    }
    return [
      {
        value: `manual||${localSettings.llm_model}`,
        label: `${localSettings.llm_model} (typed)`,
        model: localSettings.llm_model,
        provider: localSettings.llm_provider || 'ollama',
        baseUrl: localSettings.llm_base_url || '',
      },
      ...localAiModelOptions,
    ];
  }, [
    localAiModelOptions,
    localSettings.llm_base_url,
    localSettings.llm_model,
    localSettings.llm_provider,
    selectedLocalAiModelOption,
  ]);
  const localAiModelSelectValue = selectedLocalAiModelOption?.value
    || (localSettings.llm_model ? `manual||${localSettings.llm_model}` : '');
  const llmConnectorOptions = useMemo(() => {
    const fallback = currentAiSource === 'local'
      ? [
        { value: 'llm.ollama', label: 'Ollama', provider: 'ollama', baseUrl: 'http://localhost:11434', defaultModels: [], authType: 'none', requiresApiKey: false },
        { value: 'llm.openai_compatible', label: 'OpenAI-compatible local server', provider: 'openai_compatible', baseUrl: '', defaultModels: [], authType: 'none', requiresApiKey: false },
      ]
      : currentAiSource === 'codex'
        ? [{ value: 'llm.codex', label: 'ChatGPT Plus / Codex', provider: 'openai', baseUrl: '', defaultModels: [], authType: 'oauth', requiresApiKey: false }]
        : [
          { value: 'llm.openai', label: 'OpenAI', provider: 'openai', baseUrl: '', defaultModels: [], authType: 'api_key', requiresApiKey: true },
          { value: 'llm.anthropic', label: 'Anthropic', provider: 'anthropic', baseUrl: '', defaultModels: [], authType: 'api_key', requiresApiKey: true },
          { value: 'llm.google', label: 'Google Gemini', provider: 'google', baseUrl: '', defaultModels: [], authType: 'api_key', requiresApiKey: true },
          { value: 'llm.openai_compatible', label: 'OpenAI-compatible', provider: 'openai_compatible', baseUrl: '', defaultModels: [], authType: 'api_key', requiresApiKey: true },
        ];

    const manifests = llmConnectorManifests.filter((connector) => {
      if (currentAiSource === 'local') return connector.tags.includes('local') || connector.id === 'llm.openai_compatible';
      if (currentAiSource === 'byok') return connector.tags.includes('byok');
      if (currentAiSource === 'codex') return connector.id === 'llm.codex';
      return connector.id === 'llm.managed';
    });
    if (manifests.length === 0) return fallback;

    return manifests.map((connector) => {
      const provider = typeof connector.settingHints.llm_provider === 'string'
        ? connector.settingHints.llm_provider
        : connector.adapter;
      const baseUrl = typeof connector.settingHints.llm_base_url === 'string'
        ? connector.settingHints.llm_base_url
        : connector.defaultBaseUrl;
      return {
        value: connector.id,
        label: connector.label,
        provider,
        baseUrl: baseUrl || '',
        defaultModels: connector.defaultModels,
        authType: connector.authType,
        requiresApiKey: connector.requiresApiKey,
      };
    });
  }, [currentAiSource, llmConnectorManifests]);
  const selectedLlmConnectorOption = useMemo(() => {
    const selectedValue = (() => {
      const provider = localSettings.llm_provider || (currentAiSource === 'local' ? 'ollama' : 'openai');
      const baseUrl = localSettings.llm_base_url || '';
      if (provider === 'openai_compatible' && baseUrl) {
        const byBaseUrl = llmConnectorOptions.find((option) => option.provider === provider && baseUrlsMatch(option.baseUrl, baseUrl));
        if (byBaseUrl) return byBaseUrl.value;
      }
      const byProvider = llmConnectorOptions.find((option) => option.provider === provider && !option.baseUrl);
      if (byProvider) return byProvider.value;
      const byProviderAnyBase = llmConnectorOptions.find((option) => option.provider === provider);
      return byProviderAnyBase?.value || llmConnectorOptions[0]?.value || '';
    })();
    return llmConnectorOptions.find((option) => option.value === selectedValue) || null;
  }, [currentAiSource, llmConnectorOptions, localSettings.llm_base_url, localSettings.llm_provider]);
  const selectedLlmConnectorId = useMemo(() => {
    return selectedLlmConnectorOption?.value || '';
  }, [selectedLlmConnectorOption]);
  const selectedLlmConnectorManifest = useMemo(() => (
    llmConnectorManifests.find((connector) => connector.id === selectedLlmConnectorId) || null
  ), [llmConnectorManifests, selectedLlmConnectorId]);
  const selectedSavedLlmProfile = useMemo(() => (
    llmProfiles.find((profile) => profile.selected) || null
  ), [llmProfiles]);

  useEffect(() => {
    const timeoutId = setTimeout(() => setDebouncedSearchTerm(searchTerm), 180);
    return () => clearTimeout(timeoutId);
  }, [searchTerm]);

  useEffect(() => {
    if (!isOpen || !modalRef.current || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect?.width;
      if (width) setModalWidth(width);
    });
    observer.observe(modalRef.current);
    return () => observer.disconnect();
  }, [isOpen]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const updateWidth = () => {
      const width = modalRef.current?.getBoundingClientRect?.().width || Math.min(window.innerWidth, 960);
      setModalWidth(width);
    };
    updateWidth();
    window.addEventListener('resize', updateWidth);
    return () => window.removeEventListener('resize', updateWidth);
  }, []);

  const visibleTabs = useMemo(
    () => TABS.filter((tab) => matchesSearch(tab, debouncedSearchTerm)),
    [debouncedSearchTerm],
  );

  useEffect(() => {
    if (!visibleTabs.length) return;
    if (!visibleTabs.some((tab) => tab.id === activeTab)) {
      setActiveTab(visibleTabs[0].id);
    }
  }, [activeTab, visibleTabs]);

  // Lazy-load devices and playlists when their sections are active
  const [devicesFetched, setDevicesFetched] = useState(false);
  const [playlistsFetched, setPlaylistsFetched] = useState(false);
  const tabClickHandlers = useMemo(() => (
    TABS.reduce((handlers, tab) => {
      handlers[tab.id] = () => {
        if (import.meta.env.DEV) {
          performance.mark('tab-switch-start-' + tab.id);
          console.time('tab-switch-' + tab.id);
        }
        setActiveTab(tab.id);
      };
      return handlers;
    }, {})
  ), []);
  const activeTabRef = useRef('');
  React.useLayoutEffect(() => {
    if (activeTabRef.current && activeTabRef.current !== activeTab) {
      if (import.meta.env.DEV) {
        const startMark = 'tab-switch-start-' + activeTab;
        if (performance.getEntriesByName(startMark, 'mark').length > 0) {
          performance.mark('tab-switch-end-' + activeTab);
          performance.measure(
            'tab-switch-' + activeTabRef.current + '-to-' + activeTab,
            startMark,
            'tab-switch-end-' + activeTab
          );
          console.timeEnd('tab-switch-' + activeTab);
        }
      }
    }
    activeTabRef.current = activeTab;
  });

  useEffect(() => {
    if (!isOpen || activeTab !== 'music' || playlistsFetched) return undefined;

    const timeoutId = setTimeout(() => {
      refreshPlaylists();
      setPlaylistsFetched(true);
    }, MODAL_OPEN_DEFER_MS);

    return () => clearTimeout(timeoutId);
  }, [isOpen, activeTab, playlistsFetched, refreshPlaylists]);

  useEffect(() => {
    // #4226: audio-device enumeration is `/v1/settings/devices` off
    // ui/settings_api.py, which has no cloud RouteGroup at all. The System tab
    // renders the DesktopUpsell on the cloud SPA, so this was a dead fetch on
    // every open of a tab whose controls could not work anyway.
    if (isFeatureHidden('system_controls')) return undefined;
    if (!isOpen || activeTab !== 'system' || devicesFetched) return undefined;

    const timeoutId = setTimeout(() => {
      refreshDevices();
      setDevicesFetched(true);
    }, MODAL_OPEN_DEFER_MS);

    return () => clearTimeout(timeoutId);
  }, [isOpen, activeTab, devicesFetched, refreshDevices]);

  const refreshPaymentCards = useCallback(async ({ silent = false } = {}) => {
    // The payment card vault is desktop-only (encrypted local vault,
    // hard localhost-only guarded -- Tier-3, never cloud, #1064). Skip the
    // dead fetch on the cloud SPA; the section renders a DesktopUpsell.
    if (isFeatureHidden('payment_cards')) {
      setPaymentCards([]);
      setPaymentCardsLoading(false);
      return;
    }
    setPaymentCardsLoading(true);
    setPaymentCardsError('');

    try {
      const data = await apiFetch('/api/payments/cards');
      setPaymentCards(Array.isArray(data?.cards) ? data.cards : []);
    } catch {
      if (!silent) {
        setPaymentCards([]);
        setPaymentCardsError('Payment methods unavailable.');
      }
    } finally {
      setPaymentCardsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isOpen || activeTab !== 'account') return undefined;

    let cancelled = false;
    refreshPaymentCards().catch(() => {
      if (!cancelled) setPaymentCardsError('Payment methods unavailable.');
    });
    return () => {
      cancelled = true;
    };
  }, [isOpen, activeTab, refreshPaymentCards]);

  const [isSyncing, setIsSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState(null);
  const [isRefreshingDevices, setIsRefreshingDevices] = useState(false);
  const [devicesRefreshError, setDevicesRefreshError] = useState(null);
  // Volume preview — plays a brief tone so the user can hear the level
  const playVolumePreview = useCallback((volume) => {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const oscillator = ctx.createOscillator();
      const gainNode = ctx.createGain();
      oscillator.connect(gainNode);
      gainNode.connect(ctx.destination);
      oscillator.frequency.value = 440; // A4 note
      gainNode.gain.value = volume / 100 * 0.3; // Scale to reasonable level
      oscillator.start();
      oscillator.stop(ctx.currentTime + 0.15); // 150ms beep
      // Clean up
      setTimeout(() => ctx.close(), 200);
    } catch {
      // Silently ignore — audio preview is non-critical
    }
  }, []);

  const debouncedVolumePreview = useDebouncedCallback((volume) => {
    playVolumePreview(volume);
  }, 200);

  // Weekly review (services/meta_analysis/weekly_review.py)
  const [weeklyReviewSummary, setWeeklyReviewSummary] = useState(null);

  // Music source / local library state
  const [isEditingSource, setIsEditingSource] = useState(false);
  const [isBrowsing, setIsBrowsing] = useState(false);
  const [isScanning, setIsScanning] = useState(false);
  const [scanResult, setScanResult] = useState(null);
  const [localTrackCount, setLocalTrackCount] = useState(null);
  const [isLoadingTrackCount, setIsLoadingTrackCount] = useState(false);

  // Sync local settings when modal opens or settings change
  useEffect(() => {
    if (isOpen && settings && Object.keys(settings).length > 0) {
      setLocalSettings({
        ...settings,
      });
      setHasChanges(false);
    }
  }, [settings, isOpen]);

  useEffect(() => {
    if (!isOpen) return undefined;
    // C-401 follow-up: the connector CATALOG is the fourth call site into the
    // LOCAL_ONLY `connectors` group, alongside the three handlers gated below.
    // It fires on every settings open, so leaving it ungated 404s on the cloud
    // SPA every time the modal is opened, even though the section it feeds now
    // renders the DesktopUpsell and never uses the result.
    if (isFeatureHidden('desktop_ai')) {
      setLlmConnectorManifests([]);
      setLlmConnectorStatus('');
      return undefined;
    }
    let cancelled = false;
    apiFetch('/v1/connectors?category=llm')
      .then((data) => {
        if (cancelled) return;
        const connectors = normalizeConnectorManifests(data?.connectors ?? data?.data?.connectors ?? []);
        setLlmConnectorManifests(connectors);
        setLlmConnectorStatus(connectors.length > 0 ? '' : 'Provider presets unavailable; showing fallback options.');
      })
      .catch(() => {
        if (cancelled) return;
        setLlmConnectorManifests([]);
        setLlmConnectorStatus('Provider presets unavailable; showing fallback options.');
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  // Fetch local library track count when modal opens
  // Skip if a scan is in progress to avoid overwriting the scan result
  useEffect(() => {
    // #4226: `/v1/local/*` is the COMPANION_REQUIRED `local_library` /
    // `local_media` group (backend/cloud_route_manifest.py) — it reads the
    // desktop's own music filesystem, which a cloud tab has no path to.
    if (isFeatureHidden('local_music')) {
      setLocalTrackCount(null);
      setIsLoadingTrackCount(false);
      return undefined;
    }
    if (isScanning) {
      setIsLoadingTrackCount(false);
      return undefined;
    }
    if (!isOpen || settings?.active_music_provider_id !== 'local' || !settings?.local_music_folder) {
      setLocalTrackCount(null);
      setIsLoadingTrackCount(false);
      return undefined;
    }

    let cancelled = false;
    setIsLoadingTrackCount(true);
    const timeoutId = setTimeout(() => {
      apiFetch('/v1/local/library?limit=1&offset=0')
        .then(data => {
          if (cancelled) return;
          if (data != null && data.ok !== false && data.total !== undefined) {
            setLocalTrackCount(data.total);
          } else if (data?.data?.total !== undefined) {
            setLocalTrackCount(data.data.total);
          }
        })
        .catch(() => {
          if (!cancelled) setLocalTrackCount(null);
        })
        .finally(() => {
          if (!cancelled) setIsLoadingTrackCount(false);
        });
    }, MODAL_OPEN_DEFER_MS);

    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [isOpen, isScanning, settings?.active_music_provider_id, settings?.local_music_folder]);

  const hasLocalSettingsChanges = useCallback((nextSettings) => (
    JSON.stringify(nextSettings || {}) !== JSON.stringify(settings || {})
  ), [settings]);

  // Update a local setting
  const updateLocal = useCallback((key, value) => {
    setLocalSettings(prev => {
      const next = { ...prev, [key]: value };
      setHasChanges(hasLocalSettingsChanges(next));
      return next;
    });
  }, [hasLocalSettingsChanges]);

  const updateLocalSettings = useCallback((nextSettings) => {
    setLocalSettings(prev => {
      const next = typeof nextSettings === 'function' ? nextSettings(prev) : { ...prev, ...nextSettings };
      setHasChanges(hasLocalSettingsChanges(next));
      return next;
    });
  }, [hasLocalSettingsChanges]);

  const refreshLlmProfiles = useCallback(async ({ silent = false } = {}) => {
    // C-401: `/v1/connectors/profiles*` belongs to the LOCAL_ONLY `connectors`
    // group (backend/cloud_route_manifest.py) precisely because those routes
    // carry BYOK provider keys. On cloud the whole desktop_ai surface renders
    // the DesktopUpsell, so this would be a dead 404 on every settings open.
    if (isFeatureHidden('desktop_ai')) {
      setLlmProfiles([]);
      setLlmProfilesStatus('');
      return [];
    }
    try {
      const data = await apiFetch('/v1/connectors/profiles?category=llm');
      const profiles = normalizeConnectionProfiles(data?.profiles ?? data?.data?.profiles ?? []);
      setLlmProfiles(profiles);
      if (!silent) {
        setLlmProfilesStatus(profiles.length > 0 ? '' : 'No saved provider profiles yet.');
      }
      return profiles;
    } catch {
      setLlmProfiles([]);
      setLlmProfilesStatus('Saved provider profiles unavailable.');
      return [];
    }
  }, []);

  useEffect(() => {
    if (!isOpen) return undefined;
    let cancelled = false;
    refreshLlmProfiles({ silent: true }).then((profiles) => {
      if (!cancelled && profiles.length === 0) {
        setLlmProfilesStatus('');
      }
    });
    return () => {
      cancelled = true;
    };
  }, [isOpen, refreshLlmProfiles]);

  const handleLlmConnectorChange = useCallback((connectorId) => {
    const option = llmConnectorOptions.find((item) => item.value === connectorId);
    if (!option) return;
    setLlmProfileActionStatus('');
    updateLocalSettings((prev) => {
      const provider = option.provider || prev.llm_provider || (currentAiSource === 'local' ? 'ollama' : 'openai');
      const needsBaseUrl = provider === 'openai_compatible' || provider === 'ollama';
      const connectorChanged = connectorId !== selectedLlmConnectorId;
      return {
        ...prev,
        ai_source: currentAiSource,
        llm_provider: provider,
        llm_base_url: needsBaseUrl ? (option.baseUrl || prev.llm_base_url || '') : '',
        llm_model: connectorChanged ? (option.defaultModels?.[0] || '') : (prev.llm_model || option.defaultModels?.[0] || ''),
      };
    });
  }, [currentAiSource, llmConnectorOptions, selectedLlmConnectorId, updateLocalSettings]);

  const settingsForLlmProfile = useCallback((profile) => {
    const connector = llmConnectorManifests.find((item) => item.id === profile.connectorId);
    const provider = connector?.settingHints?.llm_provider || profile.adapter || localSettings.llm_provider || 'openai';
    const source = aiSourceForConnector(connector, currentAiSource);
    return {
      ai_source: source,
      llm_provider: provider,
      llm_base_url: profile.baseUrl || connector?.defaultBaseUrl || '',
      llm_model: profile.model || connector?.defaultModels?.[0] || '',
      llm_api_key: '',
    };
  }, [currentAiSource, llmConnectorManifests, localSettings.llm_provider]);

  const persistLlmSettingsPatch = useCallback(async (patch) => {
    const nextSettings = {
      ...localSettings,
      ...patch,
      llm_api_key: '',
    };
    const success = await updateSettings(nextSettings);
    if (success) {
      setLocalSettings(nextSettings);
      setHasChanges(false);
    }
    return success;
  }, [localSettings, updateSettings]);

  const handleSaveLlmProfile = useCallback(async () => {
    // C-401: this is the secret-bearing write -- `body.api_key` below carries a
    // Tier-3 BYOK provider key. The cloud surface must never post one, even if
    // a future edit re-exposes a control that reaches this handler.
    if (isFeatureHidden('desktop_ai')) {
      setLlmProfileActionStatus('Provider keys are set up in the desktop app.');
      return;
    }
    const connectorId = selectedLlmConnectorId || selectedLlmConnectorOption?.value;
    if (!connectorId) {
      setLlmProfileActionStatus('Choose a provider preset before saving a profile.');
      return;
    }
    const connector = selectedLlmConnectorManifest;
    const option = selectedLlmConnectorOption;
    const requiresApiKey = currentAiSource === 'byok' && (
      connector?.requiresApiKey
      || connector?.authType === 'api_key'
      || option?.requiresApiKey
      || option?.authType === 'api_key'
    );
    const apiKey = String(localSettings.llm_api_key || '').trim();
    const existingProfile = llmProfiles.find((profile) => (
      profile.selected && profile.connectorId === connectorId
    )) || llmProfiles.find((profile) => (
      profile.connectorId === connectorId
      && baseUrlsMatch(profile.baseUrl, localSettings.llm_base_url || option?.baseUrl || '')
      && (profile.model || '') === (localSettings.llm_model || '')
    ));

    if (requiresApiKey && !apiKey && !existingProfile?.secrets?.api_key) {
      setLlmProfileActionStatus('Paste an API key before saving this provider profile.');
      return;
    }

    setLlmProfileBusyId('save-current');
    setLlmProfileActionStatus('');
    try {
      const body = {
        connector_id: connectorId,
        profile_id: existingProfile?.profileId,
        display_name: option?.label || connector?.label || 'Provider profile',
        base_url: localSettings.llm_base_url || option?.baseUrl || connector?.defaultBaseUrl || '',
        model: localSettings.llm_model || option?.defaultModels?.[0] || connector?.defaultModels?.[0] || '',
        enabled: true,
        selected: true,
        metadata: { saved_from: 'settings_modal' },
      };
      if (apiKey) {
        body.api_key = apiKey;
      }
      const data = await apiFetch('/v1/connectors/profiles', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const savedProfile = normalizeConnectionProfiles([data?.profile ?? data?.data?.profile])[0];
      const patch = savedProfile
        ? settingsForLlmProfile(savedProfile)
        : {
          ai_source: currentAiSource,
          llm_provider: option?.provider || localSettings.llm_provider || 'openai',
          llm_base_url: body.base_url,
          llm_model: body.model,
          llm_api_key: '',
        };
      const settingsSaved = await persistLlmSettingsPatch(patch);
      await refreshLlmProfiles({ silent: true });
      setLlmProfileActionStatus(
        settingsSaved
          ? 'Provider profile saved and selected.'
          : 'Provider profile saved, but settings did not sync.'
      );
    } catch {
      setLlmProfileActionStatus('Could not save provider profile.');
    } finally {
      setLlmProfileBusyId('');
    }
  }, [
    currentAiSource,
    llmProfiles,
    localSettings.llm_api_key,
    localSettings.llm_base_url,
    localSettings.llm_model,
    localSettings.llm_provider,
    persistLlmSettingsPatch,
    refreshLlmProfiles,
    selectedLlmConnectorId,
    selectedLlmConnectorManifest,
    selectedLlmConnectorOption,
    settingsForLlmProfile,
  ]);

  const handleSelectLlmProfile = useCallback(async (profile) => {
    if (!profile?.profileId) return;
    setLlmProfileBusyId(`select:${profile.profileId}`);
    setLlmProfileActionStatus('');
    try {
      const data = await apiFetch(`/v1/connectors/profiles/${encodeURIComponent(profile.profileId)}/select`, {
        method: 'POST',
      });
      const selectedProfile = normalizeConnectionProfiles([data?.profile ?? data?.data?.profile])[0] || profile;
      const settingsSaved = await persistLlmSettingsPatch(settingsForLlmProfile(selectedProfile));
      await refreshLlmProfiles({ silent: true });
      setLlmProfileActionStatus(
        settingsSaved
          ? `Using ${selectedProfile.displayName}.`
          : `${selectedProfile.displayName} is selected, but settings did not sync.`
      );
    } catch {
      setLlmProfileActionStatus('Could not select provider profile.');
    } finally {
      setLlmProfileBusyId('');
    }
  }, [persistLlmSettingsPatch, refreshLlmProfiles, settingsForLlmProfile]);

  const handleValidateLlmProfile = useCallback(async (profile) => {
    if (!profile?.profileId) return;
    setLlmProfileBusyId(`validate:${profile.profileId}`);
    setLlmProfileActionStatus('');
    try {
      const validation = await apiFetch(`/v1/connectors/profiles/${encodeURIComponent(profile.profileId)}/validate`, {
        method: 'POST',
        body: JSON.stringify({ probe_tools: true }),
      });
      await refreshLlmProfiles({ silent: true });
      const latency = Number.isFinite(Number(validation?.latency_ms)) ? ` (${validation.latency_ms}ms)` : '';
      const probe = validation?.tool_contract?.live_tool_probe;
      const toolText = probe && typeof probe === 'object' ? ` Tools ${probe.success ? 'ok' : 'failed'}.` : '';
      setLlmProfileActionStatus(
        validation?.valid
          ? `Profile validation passed${latency}.${toolText}`
          : `Profile validation failed: ${validation?.message || validation?.error_code || 'connection test failed'}`
      );
    } catch {
      setLlmProfileActionStatus('Could not validate provider profile.');
    } finally {
      setLlmProfileBusyId('');
    }
  }, [refreshLlmProfiles]);

  const handleDetectLocalAi = useCallback(async () => {
    // C-401: local-model detection scans the user's own machine via
    // /v1/settings/detect-local-ai, which is unwired on cloud. Part of the
    // desktop_ai surface.
    if (isFeatureHidden('desktop_ai')) {
      setLocalAiDetectStatus('Local models are set up in the desktop app.');
      return;
    }
    setIsDetectingLocalAi(true);
    setLocalAiDetectStatus('');
    try {
      const data = await apiFetch('/v1/settings/detect-local-ai');
      const servers = normalizeLocalAiServers(data?.servers ?? data?.data?.servers ?? []);
      setLocalAiServers(servers);
      const detected = servers.find((server) => server.models.length > 0) || servers[0];
      if (!detected) {
        setLocalAiDetectStatus('No local servers found.');
        return;
      }
      updateLocalSettings((prev) => ({
        ...prev,
        ai_source: 'local',
        llm_provider: detected.type || prev.llm_provider || 'ollama',
        llm_base_url: detected.url || prev.llm_base_url || '',
        llm_model: detected.models.includes(prev.llm_model) ? prev.llm_model : (detected.models[0] || prev.llm_model || ''),
      }));
      const modelCount = servers.reduce((count, server) => count + server.models.length, 0);
      setLocalAiDetectStatus(
        `Detected ${detected.name || detected.type || 'local server'} with ${modelCount} installed model${modelCount === 1 ? '' : 's'}.`
        + (detected.running === false ? ' Start the local server before running commands.' : '')
      );
    } catch {
      setLocalAiDetectStatus('Local server detection failed.');
    } finally {
      setIsDetectingLocalAi(false);
    }
  }, [updateLocalSettings]);

  const openAddPaymentCardDialog = useCallback(() => {
    setPaymentCardActionError('');
    setPaymentCardForm({
      label: '',
      number: '',
      cvc: '',
      last4: '',
      exp_month: '',
      exp_year: '',
      holder_name: '',
      auth_ceiling_dollars: '',
    });
    setPaymentCardDialog({ mode: 'add', originalLabel: '' });
  }, []);

  const openEditPaymentCardDialog = useCallback((card) => {
    setPaymentCardActionError('');
    setPaymentCardForm({
      label: card.label || '',
      number: '',
      cvc: '',
      last4: card.last4 || '',
      exp_month: card.exp_month || '',
      exp_year: card.exp_year || '',
      holder_name: card.holder_name || '',
      auth_ceiling_dollars: card.auth_ceiling_cents != null
        ? String(Math.round(Number(card.auth_ceiling_cents) / 100))
        : '',
    });
    setPaymentCardDialog({ mode: 'edit', originalLabel: card.label || '' });
  }, []);

  const closePaymentCardDialog = useCallback(() => {
    if (paymentCardSaving) return;
    setPaymentCardDialog(null);
    setPaymentCardActionError('');
    setPaymentCardForm({
      label: '',
      number: '',
      cvc: '',
      last4: '',
      exp_month: '',
      exp_year: '',
      holder_name: '',
      auth_ceiling_dollars: '',
    });
  }, [paymentCardSaving]);

  const updatePaymentCardForm = useCallback((key, value) => {
    setPaymentCardForm((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleSavePaymentCard = useCallback(async () => {
    const label = paymentCardForm.label.trim();
    const number = paymentCardForm.number.replace(/\D/g, '');
    const cvc = paymentCardForm.cvc.replace(/\D/g, '');
    const last4 = paymentCardForm.last4.trim();
    const expMonth = paymentCardForm.exp_month.trim();
    const expYear = paymentCardForm.exp_year.trim();
    const holderName = paymentCardForm.holder_name.trim();
    const ceilingText = paymentCardForm.auth_ceiling_dollars.trim();
    const saveSecrets = paymentCardDialog?.mode === 'add' || number || cvc;

    if (!label || !/^\d{1,2}$/.test(expMonth) || !/^\d{2,4}$/.test(expYear)) {
      setPaymentCardActionError('Enter a label and expiration date.');
      return;
    }
    if (saveSecrets && (!/^\d{13,19}$/.test(number) || !/^\d{3,4}$/.test(cvc))) {
      setPaymentCardActionError('Enter the full card number and security code.');
      return;
    }
    if (!saveSecrets && !/^\d{4}$/.test(last4)) {
      setPaymentCardActionError('Enter the last four digits.');
      return;
    }

    const ceiling = ceilingText ? Number(ceilingText) : null;
    if (ceilingText && (!Number.isFinite(ceiling) || ceiling < 0)) {
      setPaymentCardActionError('Enter a valid authorization ceiling.');
      return;
    }

    setPaymentCardSaving(true);
    setPaymentCardActionError('');
    try {
      await apiFetch('/api/payments/cards', {
        method: 'POST',
        body: JSON.stringify({
          label,
          ...(saveSecrets ? { number, cvc } : { last4 }),
          exp_month: expMonth,
          exp_year: expYear,
          holder_name: holderName,
          auth_ceiling_cents: ceiling == null ? null : Math.round(ceiling * 100),
        }),
      });

      if (paymentCardDialog?.mode === 'edit' && paymentCardDialog.originalLabel && paymentCardDialog.originalLabel !== label) {
        await apiFetch(`/api/payments/cards/${encodeURIComponent(paymentCardDialog.originalLabel)}`, {
          method: 'DELETE',
        });
      }

      await refreshPaymentCards({ silent: true });
      setPaymentCardDialog(null);
      setPaymentCardForm({
        label: '',
        number: '',
        cvc: '',
        last4: '',
        exp_month: '',
        exp_year: '',
        holder_name: '',
        auth_ceiling_dollars: '',
      });
    } catch {
      setPaymentCardActionError("Couldn't save payment method.");
    } finally {
      setPaymentCardSaving(false);
    }
  }, [paymentCardDialog, paymentCardForm, refreshPaymentCards]);

  const handleDeletePaymentCard = useCallback(async (card) => {
    if (!card?.label) return;
    if (!window.confirm(`Delete payment method "${card.label}"?`)) return;
    setPaymentCardActionError('');
    try {
      await apiFetch(`/api/payments/cards/${encodeURIComponent(card.label)}`, {
        method: 'DELETE',
      });
      await refreshPaymentCards({ silent: true });
    } catch {
      setPaymentCardsError("Couldn't delete payment method.");
    }
  }, [refreshPaymentCards]);

  const handleOpenAdvancedSettings = useCallback(() => {
    setShowAdvancedSettings(true);
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const openAdvanced = () => setShowAdvancedSettings(true);
    window.addEventListener('viola-open-advanced-settings', openAdvanced);
    return () => window.removeEventListener('viola-open-advanced-settings', openAdvanced);
  }, []);

  // Save settings
  const handleSave = async () => {
    const weatherChanged = (localSettings.weather_location || '') !== (settings.weather_location || '');
    const success = await updateSettings(localSettings);
    if (success) {
      setHasChanges(false);
      // Refresh weather if location changed
      if (weatherChanged) {
        apiFetch('/v1/weather?force_refresh=true').catch(() => {});
      }
      onClose();
    }
  };

  // Cancel - reset local changes and close
  const handleCancel = useCallback(() => {
    setLocalSettings(settings);
    setHasChanges(false);
    clearError();
    onClose();
  }, [settings, clearError, onClose]);

  // Escape key closes the modal (a11y / keyboard parity with other modals)
  useEffect(() => {
    if (!isOpen) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        handleCancel();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, handleCancel]);

  const handleDeleteAllCallData = useCallback(async () => {
    if (!window.confirm('Delete all saved phone call history and recordings? This cannot be undone.')) {
      return;
    }
    setIsDeletingCallData(true);
    setCallDataDeleteStatus('');
    try {
      const data = await apiFetch('/v1/phone/all-history', { method: 'DELETE' });
      const deleted = Number(data?.deleted_count ?? data?.deleted ?? 0);
      setCallDataDeleteStatus(`Deleted ${deleted} call record${deleted === 1 ? '' : 's'}.`);
    } catch {
      setCallDataDeleteStatus('Could not delete call data. Try again.');
    } finally {
      setIsDeletingCallData(false);
    }
  }, []);

  // Browse for local music folder
  const handleBrowseFolder = useCallback(async () => {
    // Opens a native folder picker on the user's machine (#4226) — nothing a
    // browser tab can be shown, and the route is not served on cloud.
    if (isFeatureHidden('local_music')) return;
    setIsBrowsing(true);
    setScanResult(null);
    try {
      const data = await apiFetch('/v1/local/browse-folder', { method: 'POST' });
      const folder = data?.folder ?? data?.data?.folder;
      if (folder) {
        updateLocal('local_music_folder', folder);
      }
    } catch (err) {
      const message = err?.code === 'folder_picker_failed'
        ? "Couldn't open the folder picker. Check your desktop permissions and try again."
        : "Couldn't open the folder picker. Try again.";
      setScanResult({ ok: false, message });
    } finally {
      setIsBrowsing(false);
    }
  }, [updateLocal]);

  // Save folder and scan library (switch to local provider)
  const handleSaveAndScan = useCallback(async () => {
    if (isFeatureHidden('local_music')) return;
    const folder = localSettings.local_music_folder;
    if (!folder || !folder.trim()) return;

    setIsScanning(true);
    setScanResult(null);
    // Save settings with local provider active
    const success = await updateSettings({
      ...localSettings,
      active_music_provider_id: 'local',
      local_music_folder: folder.trim(),
    });
    if (success) {
      setHasChanges(false);
      setIsEditingSource(false);
      // Trigger rescan — endpoint now waits for completion
      try {
        const result = await apiFetch('/v1/local/library/rescan', { method: 'POST' });
        if (result == null || result.ok === false) {
          setScanResult({ ok: false, message: 'Scan failed — check folder path' });
        } else {
          const count = result.scanned ?? result.data?.scanned ?? 0;
          setLocalTrackCount(count);
          setScanResult({ ok: true, count });
        }
      } catch {
        setScanResult({ ok: false });
      }
      setIsScanning(false);
    } else {
      setIsScanning(false);
    }
  }, [localSettings, updateSettings]);

  // Music provider auth and staged source changes
  const [isSwitchingSource, setIsSwitchingSource] = useState(false);
  const [switchError, setSwitchError] = useState(null);
  const [showSpotifyLogin, setShowSpotifyLogin] = useState(false);
  const [spotifyStatus, setSpotifyStatus] = useState(null);
  const [spotifyStatusLoading, setSpotifyStatusLoading] = useState(false);
  const [spotifyStatusError, setSpotifyStatusError] = useState(null);
  const [spotifyLoginInProgress, setSpotifyLoginInProgress] = useState(false);
  const [spotifyDisconnecting, setSpotifyDisconnecting] = useState(false);

  const stageMusicProvider = useCallback((providerId) => {
    setLocalSettings(prev => {
      const next = { ...prev, active_music_provider_id: providerId };
      setHasChanges(hasLocalSettingsChanges(next));
      return next;
    });
    setScanResult(null);
    setLocalTrackCount(null);
    setSwitchError(null);
  }, [hasLocalSettingsChanges]);

  const refreshSpotifyStatus = useCallback(async ({ silent = false } = {}) => {
    // #4226: the Spotify connect flow drives a real Chrome on the user's own
    // machine over CDP — `/v1/spotify/*` is the COMPANION_REQUIRED
    // `spotify_cdp` group and `/v1/browser/auth/*` the LOCAL_ONLY
    // `browser_auth` group (backend/cloud_route_manifest.py). A headless cloud
    // backend has no local browser to drive, so none of it is served there.
    if (isFeatureHidden('music_services')) {
      setSpotifyStatus(null);
      setSpotifyStatusError(null);
      setSpotifyStatusLoading(false);
      return null;
    }
    if (!silent) {
      setSpotifyStatusLoading(true);
    }
    setSpotifyStatusError(null);
    try {
      const data = await apiFetch('/v1/spotify/status');
      setSpotifyStatus(data);
      return data;
    } catch {
      setSpotifyStatusError("Couldn't check Spotify status. Try again.");
      return null;
    } finally {
      if (!silent) {
        setSpotifyStatusLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!isOpen || activeTab !== 'music') return undefined;

    let cancelled = false;
    const timeoutId = setTimeout(async () => {
      if (!cancelled) {
        await refreshSpotifyStatus();
      }
    }, MODAL_OPEN_DEFER_MS);

    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [activeTab, isOpen, refreshSpotifyStatus]);

  // Handle "Connect with Spotify" click inside the login overlay
  const handleSpotifyConnect = useCallback(async () => {
    if (isFeatureHidden('music_services')) return;
    setSpotifyLoginInProgress(true);
    setSpotifyStatusError(null);
    setSwitchError(null);
    try {
      // Sign in through the in-app BrowserAuth overlay so the Spotify login
      // renders inside Viola's stage (QWebEngine + login overlay) — NOT a
      // separate Chrome window. Cookies are bridged to the CDP playback
      // profile, so the existing /v1/spotify/cdp/status poll below still
      // detects success.
      const loginResult = await apiFetch('/v1/browser/auth/login/spotify', { method: 'POST' });
      if (loginResult?.controller_attached === false) {
        setSpotifyStatusError('Spotify sign-in needs the Viola desktop app. Open Viola and try again.');
        return;
      }

      const startedAt = Date.now();
      while (Date.now() - startedAt < SPOTIFY_LOGIN_TIMEOUT_MS) {
        await new Promise(resolve => setTimeout(resolve, SPOTIFY_LOGIN_POLL_MS));
        const status = await refreshSpotifyStatus({ silent: true });
        if (status?.logged_in) {
          stageMusicProvider('spotify');
          setShowSpotifyLogin(false);
          return;
        }
        if (status?.login_error_code === 'identifier_rejected') {
          setSpotifyStatusError('Spotify rejected that login identifier before password entry. Use the email linked to the account.');
          return;
        }
        if (status?.challenge_required || status?.login_error_code === 'interactive_challenge_required') {
          setSpotifyStatusError('Spotify needs an extra verification step. Complete the sign-in shown in Viola, then refresh status.');
          return;
        }
      }

      setSpotifyStatusError('Sign in timed out. Try again.');
    } catch {
      setSpotifyStatusError("Couldn't open Spotify sign in. Try again.");
    } finally {
      setSpotifyLoginInProgress(false);
    }
  }, [refreshSpotifyStatus, stageMusicProvider]);

  // Handle cancel from the Spotify login overlay
  const handleSpotifyLoginCancel = useCallback(() => {
    setShowSpotifyLogin(false);
    setSpotifyStatusError(null);
  }, []);

  const handleSpotifyDisconnect = useCallback(async () => {
    if (isFeatureHidden('music_services')) return;
    setSpotifyDisconnecting(true);
    setSpotifyStatusError(null);
    try {
      await apiFetch('/v1/spotify/cdp/disconnect', { method: 'POST' });
      setSpotifyStatus(prev => ({
        ...(prev || {}),
        logged_in: false,
        connected: false,
        profile_exists: false,
        account_email: null,
        auth_source: 'none',
        cdp: {
          ...(prev?.cdp || {}),
          logged_in: false,
          connected: false,
          profile_exists: false,
        },
        cookie_bridge_cached: false,
      }));
      setShowSpotifyLogin(false);
      if ((localSettings.active_music_provider_id || 'youtube_music') === 'spotify') {
        stageMusicProvider('youtube_music');
      }
    } catch {
      setSpotifyStatusError("Couldn't disconnect Spotify. Try again.");
    } finally {
      setSpotifyDisconnecting(false);
    }
  }, [localSettings.active_music_provider_id, stageMusicProvider]);

  // Rescan existing library
  const handleRescan = useCallback(async () => {
    if (isFeatureHidden('local_music')) return;
    setIsScanning(true);
    setScanResult(null);
    try {
      const result = await apiFetch('/v1/local/library/rescan', { method: 'POST' });
      // apiFetch auto-unwraps ResponseEnvelope: returns json.data (which is null on error responses).
      // Guard against null/error: if result is null or has ok===false, the backend returned an error.
      if (result == null || result.ok === false) {
        setScanResult({ ok: false, message: 'Scan failed — check folder path' });
      } else {
        const count = result.scanned ?? result.data?.scanned ?? 0;
        setLocalTrackCount(count);
        setScanResult({ ok: true, count });
      }
    } catch {
      setScanResult({ ok: false });
    }
    setIsScanning(false);
  }, []);

  // Build device options from hook data
  const inputDeviceOptions = useMemo(() => [
    { value: '', label: 'System Default' },
    ...devices.input.map(d => ({ value: String(d.index), label: d.name })),
  ], [devices.input]);

  const outputDeviceOptions = useMemo(() => [
    { value: '', label: 'System Default' },
    ...devices.output.map(d => ({ value: String(d.index), label: d.name })),
  ], [devices.output]);

  const renderTabContent = (tabId) => {
    switch (tabId) {
      // ═══════════════════════════════════════════════════════════════
      // TAB 0: AI & AGENTS (cascading progressive disclosure)
      // ═══════════════════════════════════════════════════════════════
      case 'ai_agents': {
        const currentTier = localSettings.agent_autonomy || 'solo';
        const aiEnabled = true;
        const agentModeEnabled = localSettings.agent_enabled === true;
        // C-401: the AI-source picker, the Codex sign-in card and the Provider
        // Connection panel ARE the `desktop_ai` surface -- local model /
        // ChatGPT-Plus / BYOK provider-key config, whose credentials are Tier-3
        // (desktop-only, never cloud; CLAUDE.md "replicating any of these to
        // cloud ... makes us a credential broker"). featureSurface.js declared
        // `desktop_ai` desktop-only but nothing called the gate, so the cloud
        // SPA rendered a live `API Key` password field whose save then 404s
        // against the LOCAL_ONLY `connectors` group. Cloud renders the upsell.
        const desktopAiHidden = isFeatureHidden('desktop_ai');
        // #4226: `/v1/ai/weekly-review/*` is the LOCAL_ONLY `weekly_review`
        // group (backend/cloud_route_manifest.py) — the review is generated
        // from user-model and memory artifacts on the desktop's own disk. The
        // enable toggle was live on cloud too, so a browser user could switch
        // on a weekly review that nothing would ever run.
        const weeklyReviewHidden = isFeatureHidden('weekly_review');

        return (
          <>
            <Section title="Weekly Review">
              {weeklyReviewHidden ? (
                <div style={{ padding: '16px 20px' }}>
                  <DesktopUpsell feature="weekly_review" compact />
                </div>
              ) : (
              <>
              <SettingRow
                title="Weekly Review"
                description="Let Viola run an LLM-powered summary of your usage patterns and suggestions once a week."
                tooltip="Off by default. When enabled, Viola analyzes your memory, bug tickets, and activity weekly and files a structured summary you can review in Settings."
              >
                <Toggle
                  checked={localSettings.weekly_review_enabled ?? false}
                  onChange={(v) => updateLocal('weekly_review_enabled', v)}
                  ariaLabel="Enable weekly review"
                />
              </SettingRow>
              {localSettings.weekly_review_enabled && (
                <div style={{ padding: '4px 20px 16px', display: 'flex', gap: '10px', alignItems: 'center' }}>
                  <button
                    onClick={async () => {
                      try {
                        const resp = await apiFetch('/v1/ai/weekly-review/latest');
                        const data = await resp.json();
                        const payload = data?.data || {};
                        setWeeklyReviewSummary(payload.summary || 'No review yet.');
                      } catch (err) {
                        setWeeklyReviewSummary('Failed to load review: ' + (err?.message || err));
                      }
                    }}
                    style={{
                      padding: '6px 14px', minHeight: '44px', borderRadius: '8px', border: 'none',
                      backgroundColor: theme.colors.glassActive, color: theme.colors.textPrimary,
                      fontSize: '12px', cursor: 'pointer',
                    }}
                  >
                    View Last Review
                  </button>
                  <button
                    onClick={async () => {
                      setWeeklyReviewSummary('Running analysis…');
                      try {
                        const resp = await apiFetch('/v1/ai/weekly-review/trigger', { method: 'POST' });
                        const data = await resp.json();
                        const payload = data?.data || {};
                        if (payload.triggered && payload.analysis) {
                          setWeeklyReviewSummary(payload.analysis.summary || 'Analysis complete.');
                        } else {
                          setWeeklyReviewSummary('Analysis returned no result.');
                        }
                      } catch (err) {
                        setWeeklyReviewSummary('Trigger failed: ' + (err?.message || err));
                      }
                    }}
                    style={{
                      padding: '6px 14px', minHeight: '44px', borderRadius: '8px', border: 'none',
                      backgroundColor: theme.colors.accent, color: '#fff',
                      fontSize: '12px', cursor: 'pointer',
                    }}
                  >
                    Run Now
                  </button>
                </div>
              )}
              {weeklyReviewSummary && (
                <div style={{
                  margin: '0 20px 16px', padding: '12px 14px', borderRadius: '10px',
                  backgroundColor: theme.colors.bgElevated,
                  border: `1px solid ${theme.colors.borderSubtle}`,
                  color: theme.colors.textSecondary, fontSize: '13px',
                  whiteSpace: 'pre-wrap', lineHeight: 1.5,
                }}>
                  {weeklyReviewSummary}
                </div>
              )}
              </>
              )}
            </Section>

            {/* ── Layer 1.5: AI Source (only when AI is ON) ── */}
            {aiEnabled && (
              <Section title="AI Source">
                {desktopAiHidden ? (
                <div style={{ padding: '16px 20px' }}>
                  <DesktopUpsell feature="desktop_ai" compact />
                </div>
                ) : (
                <>
                <div style={{ padding: '16px 20px' }}>
                  <div style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                    gap: '1px',
                    borderRadius: '12px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.borderLight,
                    overflow: 'hidden',
                    }}>
                    {[
                      { id: 'managed', label: 'Viola Managed', subtitle: 'Built-in defaults' },
                      { id: 'byok', label: 'Your Own Key', subtitle: 'Bring your own API key' },
                      { id: 'codex', label: 'ChatGPT Plus', subtitle: 'Use your subscription' },
                      { id: 'local', label: 'Local Model', subtitle: 'Run on this machine' },
                    ].map((src) => {
                      const isActive = currentAiSource === src.id;
                      return (
                        <button
                          key={src.id}
                          onClick={() => {
                            if (src.id === 'local') {
                              updateLocalSettings((prev) => ({
                                ...prev,
                                ai_source: 'local',
                                llm_provider: ['ollama', 'openai_compatible'].includes(prev.llm_provider)
                                  ? prev.llm_provider
                                  : 'ollama',
                                llm_base_url: prev.llm_base_url || 'http://localhost:11434',
                              }));
                              if (localAiServers.length === 0 && !isDetectingLocalAi) {
                                void handleDetectLocalAi();
                              }
                              return;
                            }
                            updateLocal('ai_source', src.id);
                          }}
                          style={{
                            flex: 1,
                            padding: '14px 8px',
                            minHeight: '44px',
                            border: 'none',
                            backgroundColor: isActive ? theme.colors.accentActive : theme.colors.bgElevated,
                            cursor: 'pointer',
                            transition: 'all 0.15s ease',
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            gap: '2px',
                          }}
                        >
                          <span style={{
                            color: isActive ? theme.colors.accent : theme.colors.textMuted,
                            fontSize: '14px',
                            fontWeight: isActive ? 600 : 500,
                          }}>
                            {src.label}
                          </span>
                          <span style={{
                            color: isActive ? theme.colors.textSecondary : theme.colors.textMuted,
                            fontSize: '11px',
                          }}>
                            {src.subtitle}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div style={{
                  margin: '0 20px 16px',
                  padding: '12px 16px',
                  borderRadius: '12px',
                  backgroundColor: theme.colors.accentHover,
                  border: `1px solid ${theme.colors.accentBorder}`,
                }}>
                  <div style={{
                    color: theme.colors.textSecondary,
                    fontSize: '13px',
                    lineHeight: '1.5',
                    }}>
                      {currentAiSource === 'codex'
                        ? 'Uses your ChatGPT Plus subscription via the Codex CLI. No API key needed — sign in with your OpenAI account. Viola will use GPT-5.4 automatically.'
                        : currentAiSource === 'byok'
                 ? 'Enter your own provider key in the provider settings below. Conversation requests use your provider default model unless you override it, and agent tasks default to the provider-recommended agent model.'
                          : currentAiSource === 'local'
                            ? 'Runs a local model on your own machine or localhost model server. Viola does not spend managed tokens, and this mode can stay fully offline and private.'
                : 'Uses Viola-managed defaults with separate routing and agent tiers.'
                      }
                    </div>
                </div>
                </>
                )}
              </Section>
            )}

            {!desktopAiHidden && currentAiSource === 'codex' && (
              <CodexAuthCard />
            )}

            {!desktopAiHidden && ['byok', 'codex', 'local'].includes(currentAiSource) && (
              <AdvancedSection title="Provider Connection" defaultExpanded={currentAiSource === 'local'}>
                <div style={{ padding: '16px 20px', display: 'grid', gridTemplateColumns: '1fr', gap: '14px' }}>
                  <Select
                    label="Provider / preset"
                    tooltip="Choose a provider preset. Presets set the correct adapter and base URL without hiding connection state."
                    value={selectedLlmConnectorId}
                    onChange={handleLlmConnectorChange}
                    options={llmConnectorOptions}
                  />
                  {llmConnectorStatus && (
                    <div style={{ color: theme.colors.textMuted, fontSize: '12px' }}>
                      {llmConnectorStatus}
                    </div>
                  )}
                  <div>
                    <label style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                      {currentAiSource === 'local' && localAiModelSelectOptions.length > 0 ? 'Installed Model' : 'Model'}
                    </label>
                    {currentAiSource === 'local' && localAiModelSelectOptions.length > 0 ? (
                      <Select
                        value={localAiModelSelectValue}
                        onChange={(value) => {
                          const option = localAiModelSelectOptions.find((item) => item.value === value);
                          if (!option) return;
                          updateLocalSettings((prev) => ({
                            ...prev,
                            llm_provider: option.provider || prev.llm_provider || 'ollama',
                            llm_base_url: option.baseUrl || prev.llm_base_url || '',
                            llm_model: option.model,
                          }));
                        }}
                        options={localAiModelSelectOptions}
                      />
                    ) : (
                      <input
                        type="text"
                        value={localSettings.llm_model || ''}
                        onChange={(e) => {
                          updateLocalSettings((prev) => ({
                            ...prev,
                            llm_model: e.target.value,
                          }));
                        }}
                        placeholder={currentAiSource === 'local' ? 'Click "Find installed models" or type one' : 'Provider default'}
                        style={{
                          width: '100%',
                          padding: '12px 16px',
                          minHeight: '44px',
                          borderRadius: '12px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: theme.colors.bgCard,
                          color: theme.colors.textPrimary,
                          fontSize: '14px',
                          outline: 'none',
                          boxSizing: 'border-box',
                        }}
                      />
                    )}
                    {currentAiSource === 'local' && (
                      <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '6px' }}>
                        Viola checks running localhost servers and installed Ollama models on this machine. If a selected server is not running, start it before sending commands.
                      </div>
                    )}
                  </div>
                  <div>
                    <label style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                      Base URL
                    </label>
                    <input
                      type="url"
                      value={localSettings.llm_base_url || ''}
                      onChange={(e) => updateLocal('llm_base_url', e.target.value)}
                      placeholder={currentAiSource === 'local' ? 'http://localhost:11434' : 'Optional custom endpoint'}
                      style={{
                        width: '100%',
                        padding: '12px 16px',
                        minHeight: '44px',
                        borderRadius: '12px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: theme.colors.bgCard,
                        color: theme.colors.textPrimary,
                        fontSize: '14px',
                        outline: 'none',
                        boxSizing: 'border-box',
                      }}
                    />
                    {currentAiSource === 'local' && (selectedLlmConnectorManifest?.localPresets?.length > 0) && (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '8px' }}>
                        <span style={{ color: theme.colors.textMuted, fontSize: '12px', alignSelf: 'center' }}>
                          Presets:
                        </span>
                        {selectedLlmConnectorManifest.localPresets.map((preset) => (
                          <button
                            key={preset.name}
                            type="button"
                            onClick={() => updateLocal('llm_base_url', preset.base_url)}
                            title={preset.base_url}
                            style={{
                              padding: '4px 10px',
                              minHeight: '44px',
                              borderRadius: '8px',
                              border: `1px solid ${theme.colors.borderLight}`,
                              backgroundColor: baseUrlsMatch(localSettings.llm_base_url, preset.base_url)
                                ? theme.colors.accentSoft || theme.colors.bgCard
                                : theme.colors.bgCard,
                              color: theme.colors.textSecondary,
                              fontSize: '12px',
                              cursor: 'pointer',
                            }}
                          >
                            {preset.name}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {currentAiSource === 'local' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                      <button
                        type="button"
                        onClick={handleDetectLocalAi}
                        disabled={isDetectingLocalAi}
                        style={{
                          alignSelf: 'flex-start',
                          padding: '8px 14px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: isDetectingLocalAi ? theme.colors.glassBase : 'transparent',
                          color: theme.colors.textSecondary,
                          fontSize: '13px',
                          cursor: isDetectingLocalAi ? 'wait' : 'pointer',
                          opacity: isDetectingLocalAi ? 0.7 : 1,
                        }}
                      >
                        {isDetectingLocalAi ? 'Detecting...' : 'Find installed models'}
                      </button>
                      {localAiDetectStatus && (
                        <div style={{ color: theme.colors.textMuted, fontSize: '12px' }}>
                          {localAiDetectStatus}
                        </div>
                      )}
                    </div>
                  )}
                  {currentAiSource === 'byok' && (
                    <div>
                      <label style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                        API Key
                      </label>
                      <input
                        type="password"
                        value={localSettings.llm_api_key || ''}
                        onChange={(e) => updateLocal('llm_api_key', e.target.value)}
                        placeholder="Paste provider key"
                        style={{
                          width: '100%',
                          padding: '12px 16px',
                          minHeight: '44px',
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
                  )}
                  <div style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: '8px',
                    alignItems: 'center',
                  }}>
                    <button
                      type="button"
                      onClick={handleSaveLlmProfile}
                      disabled={llmProfileBusyId === 'save-current' || saving}
                      style={{
                        padding: '9px 14px',
                        minHeight: '44px',
                        borderRadius: '8px',
                        border: 'none',
                        backgroundColor: llmProfileBusyId === 'save-current' || saving
                          ? theme.colors.glassActive
                          : theme.colors.accent,
                        color: '#fff',
                        fontSize: '13px',
                        fontWeight: 600,
                        cursor: llmProfileBusyId === 'save-current' || saving ? 'wait' : 'pointer',
                        opacity: llmProfileBusyId === 'save-current' || saving ? 0.75 : 1,
                      }}
                    >
                      {llmProfileBusyId === 'save-current' ? 'Saving profile...' : 'Save & use profile'}
                    </button>
                    {selectedSavedLlmProfile && (
                      <button
                        type="button"
                        onClick={() => handleValidateLlmProfile(selectedSavedLlmProfile)}
                        disabled={llmProfileBusyId === `validate:${selectedSavedLlmProfile.profileId}`}
                        style={{
                          padding: '9px 14px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.textSecondary,
                          fontSize: '13px',
                          cursor: llmProfileBusyId === `validate:${selectedSavedLlmProfile.profileId}` ? 'wait' : 'pointer',
                          opacity: llmProfileBusyId === `validate:${selectedSavedLlmProfile.profileId}` ? 0.7 : 1,
                        }}
                      >
                        {llmProfileBusyId === `validate:${selectedSavedLlmProfile.profileId}` ? 'Testing...' : 'Test active profile'}
                      </button>
                    )}
                    <div style={{ color: theme.colors.textMuted, fontSize: '12px', lineHeight: 1.4 }}>
                      Profiles store provider config per account. API keys go to the encrypted vault, not settings JSON.
                    </div>
                  </div>
                  {(llmProfileActionStatus || llmProfilesStatus) && (
                    <div style={{
                      color: /failed|could not|paste/i.test(llmProfileActionStatus || llmProfilesStatus)
                        ? theme.colors.statusRed
                        : theme.colors.textMuted,
                      fontSize: '12px',
                    }}>
                      {llmProfileActionStatus || llmProfilesStatus}
                    </div>
                  )}
                  {llmProfiles.length > 0 && (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '8px' }}>
                      <div style={{
                        color: theme.colors.textSecondary,
                        fontSize: '13px',
                        fontWeight: 600,
                      }}>
                        Saved provider profiles
                      </div>
                      {llmProfiles.map((profile) => {
                        const isSelected = profile.selected;
                        const isSelecting = llmProfileBusyId === `select:${profile.profileId}`;
                        const isValidating = llmProfileBusyId === `validate:${profile.profileId}`;
                        return (
                          <div
                            key={profile.profileId}
                            style={{
                              padding: '12px',
                              borderRadius: '12px',
                              border: `1px solid ${isSelected ? theme.colors.accentBorder : theme.colors.borderLight}`,
                              backgroundColor: isSelected ? theme.colors.accentSubtle : theme.colors.bgCard,
                              display: 'grid',
                              gridTemplateColumns: '1fr auto',
                              gap: '10px',
                              alignItems: 'center',
                            }}
                          >
                            <div style={{ minWidth: 0 }}>
                              <div style={{
                                display: 'flex',
                                gap: '8px',
                                alignItems: 'center',
                                marginBottom: '4px',
                              }}>
                                <span style={{
                                  color: theme.colors.textPrimary,
                                  fontSize: '14px',
                                  fontWeight: 600,
                                }}>
                                  {profile.displayName}
                                </span>
                                {isSelected && (
                                  <span style={{
                                    color: theme.colors.statusGreen,
                                    fontSize: '11px',
                                    fontWeight: 700,
                                    textTransform: 'uppercase',
                                  }}>
                                    Active
                                  </span>
                                )}
                              </div>
                              <div style={{
                                color: theme.colors.textMuted,
                                fontSize: '12px',
                                lineHeight: 1.5,
                                overflowWrap: 'anywhere',
                              }}>
                                {profile.model || 'Provider default model'}
                                {profile.baseUrl ? ` - ${profile.baseUrl}` : ''}
                              </div>
                              <div style={{
                                color: profile.validation?.valid === false ? theme.colors.statusRed : theme.colors.textMuted,
                                fontSize: '12px',
                                lineHeight: 1.5,
                              }}>
                                {profile.secrets?.api_key ? 'API key saved. ' : profile.authType === 'api_key' ? 'API key not saved. ' : 'No API key required. '}
                                {validationSummary(profile)}
                              </div>
                            </div>
                            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                              <button
                                type="button"
                                onClick={() => handleSelectLlmProfile(profile)}
                                disabled={isSelected || isSelecting || !profile.enabled}
                                style={{
                                  padding: '7px 10px',
                                  minHeight: '44px',
                                  borderRadius: '8px',
                                  border: `1px solid ${theme.colors.borderLight}`,
                                  backgroundColor: isSelected ? theme.colors.glassBase : 'transparent',
                                  color: isSelected ? theme.colors.statusGreen : theme.colors.textSecondary,
                                  fontSize: '12px',
                                  cursor: isSelected || isSelecting || !profile.enabled ? 'default' : 'pointer',
                                  opacity: isSelecting || !profile.enabled ? 0.7 : 1,
                                }}
                              >
                                {isSelecting ? 'Using...' : isSelected ? 'Using' : 'Use'}
                              </button>
                              <button
                                type="button"
                                onClick={() => handleValidateLlmProfile(profile)}
                                disabled={isValidating || !profile.enabled}
                                style={{
                                  padding: '7px 10px',
                                  minHeight: '44px',
                                  borderRadius: '8px',
                                  border: `1px solid ${theme.colors.borderLight}`,
                                  backgroundColor: 'transparent',
                                  color: theme.colors.textSecondary,
                                  fontSize: '12px',
                                  cursor: isValidating || !profile.enabled ? 'wait' : 'pointer',
                                  opacity: isValidating || !profile.enabled ? 0.7 : 1,
                                }}
                              >
                                {isValidating ? 'Testing...' : 'Test'}
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </AdvancedSection>
            )}

            {/* ── Layer 2: Agent Autonomy (only when AI is ON) ── */}
            {aiEnabled && (
              <>
                <Section title="Agent Mode">
                  <SettingRow
                    title="Agent Mode"
                    description="Let Viola use local tools and browser automation when a task needs them."
                    tooltip="On by default. Turn it off and Viola answers from the conversation alone, without opening a browser or running anything on this computer."
                  >
                    <Toggle
                      checked={agentModeEnabled}
                      onChange={(v) => updateLocal('agent_enabled', v)}
                      ariaLabel="Enable agent mode"
                    />
                  </SettingRow>
                </Section>

                {agentModeEnabled && (
                  <Section title="Agent Autonomy">
                    {/* Segmented control */}
                    <div style={{ padding: '16px 20px' }}>
                      <div style={{
                        display: 'flex',
                        borderRadius: '12px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        overflow: 'hidden',
                      }}>
                        {TIER_OPTIONS.map((tier) => {
                          const isActive = currentTier === tier.id;
                          return (
                            <button
                              key={tier.id}
                              onClick={() => {
                                updateLocal('agent_autonomy', tier.id);
                                // Derive browser_session_mode from autonomy mode
                                updateLocal('browser_session_mode', TIER_BROWSER_MODE[tier.id]);
                              }}
                              style={{
                                flex: 1,
                                padding: '14px 8px',
                                minHeight: '44px',
                                border: 'none',
                                borderRight: tier.id !== 'symphony' ? `1px solid ${theme.colors.borderLight}` : 'none',
                                backgroundColor: isActive ? theme.colors.accentActive : 'transparent',
                                cursor: 'pointer',
                                transition: 'all 0.15s ease',
                                display: 'flex',
                                flexDirection: 'column',
                                alignItems: 'center',
                                gap: '2px',
                              }}
                            >
                              <span style={{
                                color: isActive ? theme.colors.accent : theme.colors.textMuted,
                                fontSize: '14px',
                                fontWeight: isActive ? 600 : 500,
                              }}>
                                {tier.label}
                              </span>
                              <span style={{
                                color: isActive ? theme.colors.textSecondary : theme.colors.textMuted,
                                fontSize: '11px',
                              }}>
                                {tier.subtitle}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </div>

                    {/* Tier description */}
                    <div style={{
                      margin: '0 20px 16px',
                      padding: '12px 16px',
                      borderRadius: '12px',
                      backgroundColor: currentTier === 'symphony'
                        ? `${theme.colors.statusYellow}12`
                        : theme.colors.accentHover,
                      border: `1px solid ${currentTier === 'symphony'
                        ? `${theme.colors.statusYellow}30`
                        : theme.colors.accentBorder}`,
                    }}>
                      <div style={{
                        color: theme.colors.textSecondary,
                        fontSize: '13px',
                        lineHeight: '1.5',
                      }}>
                        {TIER_DESCRIPTIONS[currentTier]}
                      </div>
                    </div>

                    {/* Capabilities summary grid */}
                    <div style={{
                      padding: '0 20px 16px',
                      display: 'grid',
                      gridTemplateColumns: 'repeat(3, 1fr)',
                      gap: '6px 12px',
                    }}>
                      {Object.entries(TIER_CAPABILITIES[currentTier] || {}).map(([name, enabled]) => (
                        <div key={name} style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: '6px',
                          fontSize: '12px',
                          color: enabled ? theme.colors.textSecondary : theme.colors.textMuted,
                          opacity: enabled ? 1 : 0.5,
                        }}>
                          {enabled ? (
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={theme.colors.statusGreen} strokeWidth="3" strokeLinecap="round">
                              <polyline points="20 6 9 17 4 12" />
                            </svg>
                          ) : (
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                              <line x1="18" y1="6" x2="6" y2="18" />
                              <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                          )}
                          <span>{name}</span>
                        </div>
                      ))}
                    </div>
                  </Section>
                )}
              </>
            )}
          </>
        );
      }

      case 'customize':
        return (
          <CustomizeTab
            localSettings={localSettings}
            updateLocal={updateLocal}
          />
        );

      // ═══════════════════════════════════════════════════════════════
      // TAB 1: ACCOUNT
      // ═══════════════════════════════════════════════════════════════
      case 'account': {
        return (
          <>
            {/* Account login/profile/subscription/sync — delegated to AccountTab */}
            <AccountTab
              settings={localSettings}
              onSettingChange={updateLocal}
              onDeleteAllCallData={handleDeleteAllCallData}
              isDeletingCallData={isDeletingCallData}
              callDataDeleteStatus={callDataDeleteStatus}
            />

            <Section title="Billing & Payment Methods">
              {isFeatureHidden('payment_cards') ? (
                <div style={{ padding: '16px 20px' }}>
                  <DesktopUpsell feature="payment_cards" compact />
                </div>
              ) : (
              <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px' }}>
                  <div style={{ color: theme.colors.textSecondary, fontSize: '13px' }}>
                    Saved methods are used only for local checkout assistance.
                  </div>
                  <button
                    type="button"
                    onClick={openAddPaymentCardDialog}
                    style={{
                      padding: '8px 12px',
                      minHeight: '44px',
                      borderRadius: '8px',
                      border: 'none',
                      backgroundColor: theme.colors.accent,
                      color: '#fff',
                      fontSize: '13px',
                      fontWeight: 600,
                      cursor: 'pointer',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    Add a payment method
                  </button>
                </div>
                {paymentCardsLoading && (
                  <div style={{ color: theme.colors.textMuted, fontSize: '14px' }}>
                    Loading payment methods...
                  </div>
                )}
                {!paymentCardsLoading && paymentCardsError && (
                  <div style={{ color: theme.colors.statusRed, fontSize: '14px' }}>
                    {paymentCardsError}
                  </div>
                )}
                {!paymentCardsLoading && !paymentCardsError && paymentCards.length === 0 && (
                  <div style={{ color: theme.colors.textMuted, fontSize: '14px' }}>
                    No saved payment methods.
                  </div>
                )}
                {!paymentCardsLoading && !paymentCardsError && paymentCards.map((card) => (
                  <div
                    key={`${card.label || 'card'}-${card.last4 || ''}`}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: '16px',
                      padding: '12px 14px',
                      borderRadius: '8px',
                      backgroundColor: theme.colors.bgElevated,
                      border: `1px solid ${theme.colors.borderSubtle}`,
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 600 }}>
                        {card.label || 'Saved card'}
                      </div>
                      {card.holder_name && (
                        <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '3px' }}>
                          {card.holder_name}
                        </div>
                      )}
                      {card.metadata_only && (
                        <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '3px' }}>
                          Card details are managed outside Viola.
                        </div>
                      )}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
                      <div style={{ color: theme.colors.textSecondary, fontSize: '13px', whiteSpace: 'nowrap' }}>
                        **** {card.last4 || '----'}
                        {(card.exp_month || card.exp_year) ? `  Exp ${card.exp_month || '--'}/${card.exp_year || '--'}` : ''}
                      </div>
                      <button
                        type="button"
                        title="Edit payment method"
                        aria-label={`Edit ${card.label || 'payment method'}`}
                        onClick={() => openEditPaymentCardDialog(card)}
                        style={{
                          width: '30px',
                          height: '30px',
                          minHeight: '44px',
                          minWidth: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.textSecondary,
                          cursor: 'pointer',
                          display: 'inline-flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                        }}
                      >
                        <Icons.Edit />
                      </button>
                      <button
                        type="button"
                        title="Delete payment method"
                        aria-label={`Delete ${card.label || 'payment method'}`}
                        onClick={() => handleDeletePaymentCard(card)}
                        style={{
                          width: '30px',
                          height: '30px',
                          minHeight: '44px',
                          minWidth: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.statusRed,
                          cursor: 'pointer',
                          display: 'inline-flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                        }}
                      >
                        <Icons.Trash />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
              )}
            </Section>

            {/* ── Privacy & Data Section ── */}
            <Section title="Privacy & Data">
              <SettingRow title="Usage Reports" description="Help improve Viola by sharing anonymous usage data">
                <Toggle
                  checked={localSettings.telemetry_opt_in ?? false}
                  onChange={(v) => updateLocal('telemetry_opt_in', v)}
                  ariaLabel="Enable usage reports"
                />
              </SettingRow>
              <SectionDivider />
              {/* #4789: this used to read "Sync settings and playlists across
                  devices" and write settings.json only, so it promised a thing
                  it never did and recorded consent nowhere the cloud could see.
                  Saving now moves the authoritative account row first
                  (services/sync/desktop_consent.py), and the copy claims only
                  what that row actually buys — the desktop's own Tier-2 push
                  loop is still unwired (services/sync/client.py has no
                  persistent cache/outbox/scheduler yet), which is why the last
                  clause is there rather than a vaguer sentence. */}
              <SettingRow
                title="Cloud Sync"
                description={hasCloudAccount
                  ? "Keeps your memories, playlists and synced settings in your Viola account, so the web app and phone can use them. Turning it off deletes that cloud copy. This desktop app does not upload its own settings yet."
                  : 'Sign in to your Viola account to use cloud sync.'}
              >
                <Toggle
                  checked={localSettings.consent_cloud_sync ?? false}
                  onChange={(v) => updateLocal('consent_cloud_sync', v)}
                  disabled={!hasCloudAccount}
                  ariaLabel="Cloud sync consent"
                />
              </SettingRow>
              <SectionDivider />
              <SettingRow title="Cloud Speech-to-Text" description="Allow OpenAI Whisper API transcription when Voice Recognition is set to cloud">
                <Toggle
                  checked={localSettings.consent_cloud_stt ?? false}
                  onChange={(v) => updateLocal('consent_cloud_stt', v)}
                  ariaLabel="Cloud speech-to-text consent"
                />
              </SettingRow>
              <SectionDivider />
              <SettingRow title="Error Reporting" description="Send anonymous crash reports">
                <Toggle
                  checked={localSettings.consent_error_reporting ?? false}
                  onChange={(v) => updateLocal('consent_error_reporting', v)}
                  ariaLabel="Error reporting consent"
                />
              </SettingRow>
              <SectionDivider />
              {/* #2600: desktop crash telemetry sink. This is the disclosure + opt-out
                  surface for diagnostics.diagnostic_consent's anonymized crash baseline
                  (separate from the legacy "Error Reporting" toggle above, which gates a
                  direct Sentry SDK send that needs a reachable DSN desktop doesn't have by
                  design -- see core/sentry_integration._sentry_requires_consent). Turning
                  this ON both opts in (diagnostics_baseline_opted_out=false) AND
                  acknowledges the disclosure (diagnostics_disclosure_shown=true) in one
                  step, since this row's own description IS the disclosure. Sending itself
                  additionally requires the fleet-level master flag
                  (diagnostics_baseline_enabled); until that is on, this toggle safely
                  records consent but nothing leaves the device. */}
              <SettingRow
                title="Automatic Crash Reports"
                description="When Viola crashes or hits an unexpected error, send a small anonymized report (app version, error type, redacted state) so we can fix it. Never includes recordings, conversation content, files, or account credentials."
              >
                <Toggle
                  checked={!(localSettings.diagnostics_baseline_opted_out ?? false)}
                  onChange={(v) => {
                    updateLocal('diagnostics_baseline_opted_out', !v);
                    if (v) updateLocal('diagnostics_disclosure_shown', true);
                  }}
                  ariaLabel="Automatic anonymized crash report consent"
                />
              </SettingRow>
              <SectionDivider />
              <SettingRow title="Session Replay" description="Attach masked interaction replay to Sentry reports">
                <Toggle
                  checked={localSettings.consent_session_replay ?? false}
                  onChange={(v) => updateLocal('consent_session_replay', v)}
                  ariaLabel="Session replay consent"
                />
              </SettingRow>
              <SectionDivider />
            <SettingRow title="Wake-Word Training Contribution" description="Wake-word sample sharing is unavailable. Installed wake-word models still run locally.">
                <span
                  style={{
                    padding: '6px 10px',
                    borderRadius: '999px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.bgElevated,
                    color: theme.colors.textSecondary,
                    fontSize: '12px',
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                  }}
                >
              Unavailable
                </span>
              </SettingRow>
            </Section>

            {/* ── About Section ── */}
            <Section title="About">
              <div style={{ padding: '16px 20px' }}>
                <InfoRow label="Version" value={APP_VERSION} />
                <InfoRow label="Service" value="Connected" valueColor={theme.colors.statusGreen} />
                <InfoRow label="Last Updated" value={
                  settings._settings_last_saved
                    ? new Date(settings._settings_last_saved).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
                    : 'Never'
                } />
              </div>
            </Section>
          </>
        );
      }

      // ═══════════════════════════════════════════════════════════════
      // TAB 2: MUSIC & VOICE
      // ═══════════════════════════════════════════════════════════════
      case 'music': {
        const activeProvider = localSettings.active_music_provider_id || 'youtube_music';
        const savedProvider = settings?.active_music_provider_id || 'youtube_music';
        const providerHasPendingChange = activeProvider !== savedProvider;
        // #4226: on the cloud SPA neither of these two sources can work —
        // Spotify sign-in drives a real browser on the user's machine
        // (`spotify_cdp` COMPANION_REQUIRED / `browser_auth` LOCAL_ONLY) and
        // Local Files reads the desktop's own disk (`local_library` /
        // `local_media` COMPANION_REQUIRED). YouTube is cloud-native and stays.
        const musicServicesHidden = isFeatureHidden('music_services');
        const localMusicHidden = isFeatureHidden('local_music');
        // Forced false on the cloud SPA (#4226) so that a user whose SAVED
        // provider is already 'local' or 'spotify' — set on their desktop and
        // synced down — does not get the folder-picker / rescan / Spotify
        // connect blocks rendered into a browser tab that cannot serve them.
        // Dropping the tiles alone would only stop NEW selections.
        const isLocal = activeProvider === 'local' && !localMusicHidden;
        const isSpotify = activeProvider === 'spotify' && !musicServicesHidden;
        const localFolder = localSettings.local_music_folder || '';
        const spotifyLoggedIn = Boolean(spotifyStatus?.logged_in);
        const spotifyAccountLabel = spotifyStatus?.account_email
          ? `Connected - ${spotifyStatus.account_email}`
          : 'Connected';

        // Offering these as pickable tiles let a browser user select a source
        // that could never play anything.
        const providerOptions = [
          { id: 'youtube_music', label: 'YouTube', icon: <Icons.YouTube />, desc: 'Stream from YouTube' },
          ...(musicServicesHidden ? [] : [{
            id: 'spotify',
            label: 'Spotify',
            icon: <span style={{ fontSize: '18px' }}>S</span>,
            desc: spotifyLoggedIn ? 'Connected' : 'Sign in to play Spotify',
          }]),
          ...(localMusicHidden ? [] : [
            { id: 'local', label: 'Local Files', icon: <Icons.Music />, desc: localFolder || 'Play from local library' },
          ]),
        ];
        const handleSwitchProvider = async (providerId) => {
          if (providerId === activeProvider) return;

          // Spotify: check CDP auth status before switching
          if (providerId === 'spotify') {
            setSwitchError(null);
            setIsSwitchingSource(true);
            const statusData = spotifyLoggedIn
              ? spotifyStatus
              : await refreshSpotifyStatus({ silent: true });
            if (!statusData?.logged_in) {
              setIsSwitchingSource(false);
              setShowSpotifyLogin(true);
              return;
            }
            setIsSwitchingSource(false);
          }

          stageMusicProvider(providerId);
          setShowSpotifyLogin(false);
        };

        return (
          <>
            {/* ── Music Source Selector ── */}
            <Section title="Music Source">
              <div style={{ padding: '20px' }}>
                {/* Three-way provider selector */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: '8px', marginBottom: (isLocal && localFolder) || isEditingSource ? '16px' : 0 }}>
                  {providerOptions.map(opt => {
                    const isActive = opt.id === activeProvider;
                    return (
                      <button
                        key={opt.id}
                        onClick={() => handleSwitchProvider(opt.id)}
                        disabled={isSwitchingSource}
                        style={{
                          flex: 1,
                          display: 'flex',
                          flexDirection: 'column',
                          alignItems: 'center',
                          gap: '6px',
                          padding: '12px 8px',
                          minHeight: '44px',
                          borderRadius: '10px',
                          border: isActive
                            ? `2px solid ${theme.colors.accentBorder}`
                            : `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: isActive ? theme.colors.accentSubtle : 'transparent',
                          color: isActive ? theme.colors.accent : theme.colors.textMuted,
                          fontSize: '12px',
                          fontWeight: isActive ? 600 : 400,
                          cursor: isSwitchingSource ? 'not-allowed' : 'pointer',
                          opacity: isSwitchingSource ? 0.6 : 1,
                          transition: 'all 0.15s ease',
                        }}
                      >
                        <div style={{
                          width: '32px',
                          height: '32px',
                          borderRadius: '8px',
                          backgroundColor: isActive ? theme.colors.accentActive : `${theme.colors.textMuted}10`,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          color: isActive ? theme.colors.accent : theme.colors.textMuted,
                        }}>
                          {opt.icon}
                        </div>
                        <span>{opt.label}</span>
                        <span style={{
                          fontSize: '11px',
                          lineHeight: 1.2,
                          color: isActive ? theme.colors.accent : theme.colors.textMuted,
                          minHeight: '13px',
                        }}>
                          {opt.id === 'spotify' && spotifyStatusLoading ? 'Checking' : opt.desc}
                        </span>
                      </button>
                    );
                  })}
                </div>

                {providerHasPendingChange && (
                  <div style={{
                    color: theme.colors.statusYellow,
                    fontSize: '12px',
                    marginBottom: '8px',
                  }}>
                    Unsaved music source
                  </div>
                )}

                {/* Active provider details */}
                {isLocal && localFolder && (
                  <div style={{
                    color: theme.colors.textMuted,
                    fontSize: '13px',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    marginBottom: '8px',
                  }}>
                    {localFolder}
                  </div>
                )}
                {isLocal && (isLoadingTrackCount || localTrackCount !== null) && (
                  <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginBottom: '8px' }}>
                    {isLoadingTrackCount ? 'Loading track count...' : `${localTrackCount} track${localTrackCount !== 1 ? 's' : ''} indexed`}
                  </div>
                )}

                {isLocal && (
                  <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                    <button
                      onClick={handleRescan}
                      disabled={isScanning}
                      style={{
                        padding: '8px 16px',
                        minHeight: '44px',
                        borderRadius: '8px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: 'transparent',
                        color: theme.colors.textSecondary,
                        fontSize: '13px',
                        fontWeight: 500,
                        cursor: isScanning ? 'not-allowed' : 'pointer',
                        opacity: isScanning ? 0.6 : 1,
                        transition: 'all 0.15s ease',
                      }}
                    >
                      {isScanning ? 'Scanning...' : 'Rescan Library'}
                    </button>
                    {!localFolder && (
                      <button
                        onClick={() => setIsEditingSource(true)}
                        style={{
                          padding: '8px 16px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.textSecondary,
                          fontSize: '13px',
                          fontWeight: 500,
                          cursor: 'pointer',
                          transition: 'all 0.15s ease',
                        }}
                      >
                        Set Folder
                      </button>
                    )}
                  </div>
                )}

                {(isSpotify || spotifyLoggedIn) && !showSpotifyLogin && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginTop: '8px' }}>
                    <div style={{
                      color: spotifyLoggedIn ? theme.colors.statusGreen : theme.colors.textMuted,
                      fontSize: '13px',
                      fontWeight: spotifyLoggedIn ? 600 : 400,
                    }}>
                      {spotifyStatusLoading
                        ? 'Checking Spotify...'
                        : spotifyLoggedIn
                          ? spotifyAccountLabel
                          : 'Not connected'}
                    </div>
                    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                      {spotifyLoggedIn ? (
                        <button
                          onClick={handleSpotifyDisconnect}
                          disabled={spotifyDisconnecting}
                          style={{
                            padding: '8px 14px',
                            minHeight: '44px',
                            borderRadius: '8px',
                            border: `1px solid ${theme.colors.borderLight}`,
                            backgroundColor: 'transparent',
                            color: theme.colors.textSecondary,
                            fontSize: '13px',
                            fontWeight: 500,
                            cursor: spotifyDisconnecting ? 'wait' : 'pointer',
                            opacity: spotifyDisconnecting ? 0.6 : 1,
                            transition: 'all 0.15s ease',
                          }}
                        >
                          {spotifyDisconnecting ? 'Disconnecting...' : 'Disconnect'}
                        </button>
                      ) : (
                        <button
                          onClick={() => setShowSpotifyLogin(true)}
                          style={{
                            padding: '8px 14px',
                            minHeight: '44px',
                            borderRadius: '8px',
                            border: 'none',
                            backgroundColor: theme.colors.accent,
                            color: '#fff',
                            fontSize: '13px',
                            fontWeight: 600,
                            cursor: 'pointer',
                            transition: 'all 0.15s ease',
                          }}
                        >
                          Sign In with Spotify
                        </button>
                      )}
                      <button
                        onClick={() => refreshSpotifyStatus()}
                        disabled={spotifyStatusLoading}
                        style={{
                          padding: '8px 14px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.textSecondary,
                          fontSize: '13px',
                          fontWeight: 500,
                          cursor: spotifyStatusLoading ? 'wait' : 'pointer',
                          opacity: spotifyStatusLoading ? 0.6 : 1,
                          transition: 'all 0.15s ease',
                        }}
                      >
                        {spotifyStatusLoading ? 'Refreshing...' : 'Refresh'}
                      </button>
                    </div>
                  </div>
                )}

                {/* Spotify login overlay — shown when user switches to Spotify without auth */}
                {showSpotifyLogin && (
                  <div style={{
                    marginTop: '12px',
                    padding: '20px',
                    borderRadius: '12px',
                    border: `1px solid ${theme.colors.accentBorder}`,
                    backgroundColor: theme.colors.accentHover,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px' }}>
                      <div style={{
                        width: '40px',
                        height: '40px',
                        borderRadius: '10px',
                        backgroundColor: theme.colors.accentSubtle,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: theme.colors.accent,
                        flexShrink: 0,
                      }}>
                        <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
                          <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
                        </svg>
                      </div>
                      <div>
                        <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 600 }}>
                          Connect your Spotify account
                        </div>
                        <div style={{ color: theme.colors.textMuted, fontSize: '13px', marginTop: '2px' }}>
                          A secure Chrome window will open for you to sign in to Spotify.
                        </div>
                      </div>
                    </div>

                    {spotifyLoginInProgress && (
                      <div style={{
                        padding: '10px 14px',
                        borderRadius: '8px',
                        backgroundColor: theme.colors.accentSubtle,
                        color: theme.colors.textSecondary,
                        fontSize: '13px',
                        marginBottom: '12px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                      }}>
                        <span data-essential-motion="spin-fast" style={{
                          display: 'inline-block',
                          width: '14px',
                          height: '14px',
                          border: `2px solid ${theme.colors.textMuted}`,
                          borderTopColor: theme.colors.textPrimary,
                          borderRadius: '50%',
                          animation: 'spin 0.8s linear infinite',
                          flexShrink: 0,
                        }} />
                        Waiting for login in Chrome...
                      </div>
                    )}

                    <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
                      <button
                        onClick={handleSpotifyConnect}
                        disabled={spotifyLoginInProgress}
                        style={{
                          padding: '10px 20px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: 'none',
                          backgroundColor: spotifyLoginInProgress ? theme.colors.glassActive : theme.colors.accent,
                          color: spotifyLoginInProgress ? theme.colors.textSecondary : '#fff',
                          fontSize: '14px',
                          fontWeight: 600,
                          cursor: spotifyLoginInProgress ? 'wait' : 'pointer',
                          transition: 'all 0.15s ease',
                        }}
                      >
                        {spotifyLoginInProgress ? 'Connecting...' : 'Sign In with Spotify'}
                      </button>
                      <button
                        onClick={handleSpotifyLoginCancel}
                        style={{
                          padding: '10px 16px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.textSecondary,
                          fontSize: '13px',
                          fontWeight: 500,
                          cursor: 'pointer',
                          transition: 'all 0.15s ease',
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {(switchError || spotifyStatusError) && (
                  <div style={{
                    marginTop: '12px',
                    padding: '10px 14px',
                    borderRadius: '8px',
                    backgroundColor: `${theme.colors.statusRed}10`,
                    color: theme.colors.statusRed,
                    fontSize: '13px',
                  }}>
                    {switchError || spotifyStatusError}
                  </div>
                )}

                {scanResult && (
                  <div style={{
                    marginTop: '12px',
                    padding: '10px 14px',
                    borderRadius: '8px',
                    backgroundColor: scanResult.ok
                      ? `${theme.colors.statusGreen}10`
                      : `${theme.colors.statusRed}10`,
                    color: scanResult.ok ? theme.colors.statusGreen : theme.colors.statusRed,
                    fontSize: '13px',
                  }}>
                    {scanResult.ok
                      ? (scanResult.count === 0
                        ? 'No audio files found in this folder'
                        : scanResult.count !== null
                          ? `Scan complete \u2014 ${scanResult.count} track${scanResult.count !== 1 ? 's' : ''} found`
                          : 'Scan started')
                      : getSafeErrorMessage(scanResult.message, 'Scan failed')}
                  </div>
                )}

                {isEditingSource && isLocal && (
                  <div style={{
                    marginTop: '16px',
                    padding: '16px',
                    borderRadius: '12px',
                    backgroundColor: theme.colors.bgSurface,
                    border: `1px solid ${theme.colors.borderLight}`,
                  }}>
                    <div style={{ color: theme.colors.textSecondary, fontSize: '14px', marginBottom: '12px' }}>
                      Set up Local Files
                    </div>
                    <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
                      <input
                        type="text"
                        value={localSettings.local_music_folder || ''}
                        onChange={(e) => updateLocal('local_music_folder', e.target.value)}
                        placeholder="C:\Users\Music"
                        style={{
                          flex: 1,
                          padding: '10px 14px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: theme.colors.bgCard,
                          color: theme.colors.textPrimary,
                          fontSize: '14px',
                          outline: 'none',
                          boxSizing: 'border-box',
                        }}
                      />
                      <button
                        onClick={handleBrowseFolder}
                        disabled={isBrowsing}
                        style={{
                          padding: '10px 16px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: theme.colors.glassBase,
                          color: theme.colors.textPrimary,
                          fontSize: '13px',
                          fontWeight: 500,
                          cursor: isBrowsing ? 'not-allowed' : 'pointer',
                          opacity: isBrowsing ? 0.6 : 1,
                          transition: 'all 0.15s ease',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {isBrowsing ? 'Opening...' : 'Browse...'}
                      </button>
                    </div>
                    <div style={{ display: 'flex', gap: '8px' }}>
                      <button
                        onClick={handleSaveAndScan}
                        disabled={!localSettings.local_music_folder?.trim() || isScanning}
                        style={{
                          padding: '10px 20px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: 'none',
                          backgroundColor: (!localSettings.local_music_folder?.trim() || isScanning) ? theme.colors.glassActive : theme.colors.accent,
                          color: (!localSettings.local_music_folder?.trim() || isScanning) ? theme.colors.textMuted : (theme.colors.textBright || '#ffffff'),
                          fontSize: '13px',
                          fontWeight: 600,
                          cursor: (!localSettings.local_music_folder?.trim() || isScanning) ? 'not-allowed' : 'pointer',
                          transition: 'all 0.15s ease',
                        }}
                      >
                        {isScanning ? 'Scanning...' : 'Save & Scan'}
                      </button>
                      <button
                        onClick={() => {
                          setIsEditingSource(false);
                          setLocalSettings(prev => ({ ...prev, local_music_folder: settings.local_music_folder || '' }));
                        }}
                        style={{
                          padding: '10px 16px',
                          minHeight: '44px',
                          borderRadius: '8px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: 'transparent',
                          color: theme.colors.textSecondary,
                          fontSize: '13px',
                          fontWeight: 500,
                          cursor: 'pointer',
                          transition: 'all 0.15s ease',
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </Section>

            {/* ── Playback ── */}
            <Section title="Playback">
              <div style={{ padding: '16px 20px' }}>
                <Slider
                  label="Default Volume"
                  value={localSettings.default_music_volume ?? 80}
                  onChange={(v) => {
                    updateLocal('default_music_volume', v);
                    debouncedVolumePreview(v);
                  }}
                  min={0}
                  max={100}
                />
              </div>
              <SectionDivider />
              <SettingRow title="Autoplay Enabled" description="Automatically play next track in queue" tooltip="When the current track ends, Viola automatically starts the next one in the queue.">
                <Toggle
                  checked={localSettings.autoplay_enabled ?? true}
                  onChange={(v) => updateLocal('autoplay_enabled', v)}
                  ariaLabel="Enable autoplay"
                />
              </SettingRow>
              <SectionDivider />
              <SettingRow title="AI-Powered Autoplay" description="Automatically suggest new music when the queue runs out" tooltip="When the queue is empty, Viola picks music similar to what you have been listening to and keeps playing.">
                <Toggle
                  checked={localSettings.ai_autoplay_enabled ?? true}
                  onChange={(v) => updateLocal('ai_autoplay_enabled', v)}
                  ariaLabel="Enable AI-powered autoplay"
                />
              </SettingRow>
            </Section>

            <Section title="Playlists">
              {/* Playlists management */}
              <div style={{ padding: '16px 20px' }}>
                <div style={{ marginBottom: '16px' }}>
                  <label style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                    Add Playlist URL
                  </label>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <input
                      type="text"
                      placeholder="YouTube or Spotify playlist URL"
                      id="playlist-url-input"
                      style={{
                        flex: 1,
                        padding: '12px 16px',
                        minHeight: '44px',
                        borderRadius: '12px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: theme.colors.bgCard,
                        color: theme.colors.textPrimary,
                        fontSize: '14px',
                        outline: 'none',
                      }}
                    />
                    <button
                      onClick={() => {
                        const input = document.getElementById('playlist-url-input');
                        if (input?.value) {
                          const pl = localSettings.saved_playlists || [];
                          updateLocal('saved_playlists', [...pl, { url: input.value, name: `Playlist ${pl.length + 1}` }]);
                          input.value = '';
                        }
                      }}
                      style={{
                        padding: '12px 20px',
                        minHeight: '44px',
                        borderRadius: '12px',
                        border: 'none',
                        backgroundColor: theme.colors.accent,
                        color: theme.colors.textBright || '#ffffff',
                        fontSize: '14px',
                        fontWeight: 500,
                        cursor: 'pointer',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      Add
                    </button>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginTop: '8px' }}>
                  <button
                    onClick={async () => {
                      setIsSyncing(true);
                      setSyncResult(null);
                      const result = await syncPlaylists();
                      setSyncResult(result);
                      setIsSyncing(false);
                    }}
                    disabled={isSyncing}
                    style={{
                      padding: '12px 20px',
                      minHeight: '44px',
                      borderRadius: '12px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: 'transparent',
                      color: theme.colors.textSecondary,
                      fontSize: '14px',
                      cursor: isSyncing ? 'not-allowed' : 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '8px',
                      opacity: isSyncing ? 0.6 : 1,
                      transition: 'all 0.15s ease',
                    }}
                  >
                    <Icons.YouTube />
                    {isSyncing ? 'Syncing...' : 'Sync from YouTube'}
                  </button>
                  {syncResult && (
                    <span style={{
                      fontSize: '13px',
                      color: syncResult.ok ? theme.colors.statusGreen : theme.colors.statusRed,
                    }}>
                      {syncResult.ok
                        ? `Synced ${syncResult.synced_count ?? 0} playlist${(syncResult.synced_count ?? 0) === 1 ? '' : 's'}`
                        : getSafeErrorMessage(syncResult.error, 'Sync failed')}
                    </span>
                  )}
                </div>

                {playlists.length > 0 && (
                  <div style={{ marginTop: '16px' }}>
                    <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginBottom: '8px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                      Saved Playlists
                    </div>
                    {playlists.map((playlist) => (
                      <div
                        key={playlist.name}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          padding: '12px',
                          backgroundColor: theme.colors.bgSurface,
                          borderRadius: '8px',
                          marginBottom: '8px',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1, minWidth: 0 }}>
                          <button
                            onClick={() => setDefaultPlaylist(playlist.name)}
                            title={playlist.is_default ? 'Default playlist' : 'Set as default'}
                            style={{
                              background: 'none',
                              border: 'none',
                              cursor: 'pointer',
                              color: playlist.is_default ? theme.colors.statusYellow : theme.colors.textMuted,
                              padding: '2px',
                              minHeight: '44px',
                              minWidth: '44px',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              flexShrink: 0,
                              opacity: playlist.is_default ? 1 : 0.5,
                              transition: 'all 0.15s ease',
                            }}
                          >
                            <Icons.Star filled={playlist.is_default} />
                          </button>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                              <span style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 500 }}>
                                {playlist.name}
                              </span>
                              {playlist.is_default && (
                                <span style={{
                                  fontSize: '10px',
                                  fontWeight: 600,
                                  color: theme.colors.statusYellow,
                                  backgroundColor: `${theme.colors.statusYellow}15`,
                                  padding: '2px 6px',
                                  borderRadius: '4px',
                                  textTransform: 'uppercase',
                                  letterSpacing: '0.5px',
                                }}>
                                  Default
                                </span>
                              )}
                            </div>
                            {playlist.url && (
                              <div style={{ color: theme.colors.textMuted, fontSize: '12px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {playlist.url}
                              </div>
                            )}
                          </div>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginLeft: '12px', flexShrink: 0 }}>
                          <button
                            onClick={() => {
                              const newName = window.prompt('Rename playlist:', playlist.name);
                              if (newName && newName !== playlist.name) {
                                renamePlaylist(playlist.name, newName);
                              }
                            }}
                            title="Rename"
                            style={{
                              width: '32px',
                              height: '32px',
                              minHeight: '44px',
                              minWidth: '44px',
                              borderRadius: '6px',
                              border: `1px solid ${theme.colors.borderLight}`,
                              backgroundColor: 'transparent',
                              color: theme.colors.textMuted,
                              cursor: 'pointer',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              transition: 'all 0.15s ease',
                            }}
                          >
                            <Icons.Edit />
                          </button>
                          <button
                            onClick={() => {
                              if (window.confirm(`Delete playlist "${playlist.name}"?`)) {
                                deletePlaylist(playlist.name);
                              }
                            }}
                            title="Delete"
                            style={{
                              width: '32px',
                              height: '32px',
                              minHeight: '44px',
                              minWidth: '44px',
                              borderRadius: '6px',
                              border: `1px solid ${theme.colors.statusRed}40`,
                              backgroundColor: 'transparent',
                              color: theme.colors.statusRed,
                              cursor: 'pointer',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              transition: 'all 0.15s ease',
                            }}
                          >
                            <Icons.Trash />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </Section>

            {/* Multi-room speaker setup and QR codes have moved to the Rooms page */}
          </>
        );
      }

      // ═══════════════════════════════════════════════════════════════
      // TAB: MESSAGING
      // ═══════════════════════════════════════════════════════════════
      case 'voice': {
        const sttEngineValue = localSettings.stt_engine || 'whisper_local';
        return (
          <>
            {handsFreeSurfaceSupported && (
              <Section title="Hands-Free (This Browser)">
                <SettingRow
                  title='Hands-free "Viola"'
                  description='Keep the microphone on in this tab and start listening when you say "Viola". Detection runs entirely on this device — no audio leaves it until the wake word is heard. Applies immediately, this browser only.'
                >
                  <Toggle
                    checked={handsFreeWake}
                    onChange={(v) => setHandsFreeWake(Boolean(v))}
                    ariaLabel="Enable hands-free wake word in this browser"
                  />
                </SettingRow>
              </Section>
            )}

            <Section title="Wake Word Sensitivity">
              <div style={{ padding: '16px 20px' }}>
                <Slider
                  label="Wake-Up Sensitivity"
                  tooltip="How easily Viola wakes up when you say its name. Higher means easier wake-ups and more accidental triggers."
                  value={localSettings.wake_sensitivity ?? 0.9}
                  onChange={(v) => updateLocal('wake_sensitivity', v)}
                  min={0}
                  max={1}
                  step={0.1}
                />
                <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '12px' }}>
                  Wake-word selection and custom training live in Customize.
                </div>
              </div>
            </Section>

            <Section title="Push-to-Talk">
              <div style={{ padding: '16px 20px' }}>
                {/*
                  `localSettings` is {} until the settings fetch resolves, so the fallback
                  below is what every user sees during the load window. It has to match
                  config/defaults.py DEFAULT_VOICE_MODE, or the control shows the wrong
                  mode as selected on an install that is actually wake-word.
                */}
                <Select
                  label="Voice Input Mode"
                  tooltip="Choose how Viola starts listening."
                  value={localSettings.voice_mode || 'wake_word'}
                  onChange={(v) => updateLocal('voice_mode', v)}
                  options={[
                    { value: 'push_to_talk', label: 'Push-to-Talk' },
                    { value: 'wake_word', label: 'Wake Word' },
                    { value: 'disabled', label: 'Disabled' },
                  ]}
                />
              </div>
              <SectionDivider />
              <div style={{ padding: '16px 20px' }}>
                <label
                  style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}
                  title="Choose the keyboard shortcut that starts listening when wake word detection is off."
                >
                  Push-to-Talk Shortcut
                </label>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <input
                    type="text"
                    value={localSettings.ptt_hotkey || DEFAULT_PTT_HOTKEY}
                    readOnly
                    placeholder="Press a key..."
                    onKeyDown={(e) => {
                      e.preventDefault();
                      updateLocal('ptt_hotkey', hotkeyFromKeyboardEvent(e));
                    }}
                    style={{
                      flex: 1,
                      padding: '12px 16px',
                      minHeight: '44px',
                      borderRadius: '12px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgCard,
                      color: theme.colors.textPrimary,
                      fontSize: '14px',
                      outline: 'none',
                      cursor: 'pointer',
                    }}
                  />
                  <button
                    onClick={() => updateLocal('ptt_hotkey', DEFAULT_PTT_HOTKEY)}
                    aria-label="Reset hotkey to default"
                    style={{
                      padding: '12px 16px',
                      minHeight: '44px',
                      borderRadius: '12px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: 'transparent',
                      color: theme.colors.textSecondary,
                      fontSize: '13px',
                      cursor: 'pointer',
                    }}
                  >
                    Reset
                  </button>
                </div>
                <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '8px' }}>
                  Click and press a key combination to set.
                </div>
              </div>
              <SectionDivider />
              <SettingRow title="Mute Microphone" description="Hard-gates the wake word listener and speech-to-text, and blocks push-to-talk, until you unmute.">
                <Toggle
                  checked={Boolean(localSettings.mic_muted)}
                  onChange={(v) => updateLocal('mic_muted', Boolean(v))}
                  ariaLabel="Mute microphone"
                />
              </SettingRow>
              <SectionDivider />
              <div style={{ padding: '16px 20px' }}>
                <label
                  style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}
                  title="Choose the keyboard shortcut that mutes the microphone (mic mute / pause wake word)."
                >
                  Mute Shortcut
                </label>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <input
                    type="text"
                    value={localSettings.mute_hotkey || DEFAULT_MUTE_HOTKEY}
                    readOnly
                    placeholder="Press a key..."
                    onKeyDown={(e) => {
                      e.preventDefault();
                      updateLocal('mute_hotkey', hotkeyFromKeyboardEvent(e));
                    }}
                    style={{
                      flex: 1,
                      padding: '12px 16px',
                      minHeight: '44px',
                      borderRadius: '12px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgCard,
                      color: theme.colors.textPrimary,
                      fontSize: '14px',
                      outline: 'none',
                      cursor: 'pointer',
                    }}
                  />
                  <button
                    onClick={() => updateLocal('mute_hotkey', DEFAULT_MUTE_HOTKEY)}
                    aria-label="Reset mute hotkey to default"
                    style={{
                      padding: '12px 16px',
                      minHeight: '44px',
                      borderRadius: '12px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: 'transparent',
                      color: theme.colors.textSecondary,
                      fontSize: '13px',
                      cursor: 'pointer',
                    }}
                  >
                    Reset
                  </button>
                </div>
                <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '8px' }}>
                  Click and press a key combination to set. Must be different from the Push-to-Talk Shortcut above.
                </div>
              </div>
            </Section>

            <Section title="Text to Speech">
              <SettingRow title="Speak Responses Aloud" description="When off, Viola responds with text only">
                <Toggle
                  checked={localSettings.tts_enabled ?? true}
                  onChange={(v) => updateLocal('tts_enabled', v)}
                  ariaLabel="Speak responses aloud"
                />
              </SettingRow>
              {(localSettings.tts_enabled ?? true) && (
                <>
                  <SectionDivider />
                  <SettingRow
                    title="Speak Typed Replies Too"
                    description="When off, Viola only speaks answers to things you said out loud"
                  >
                    <Toggle
                      checked={localSettings.speak_all_replies ?? true}
                      onChange={(v) => updateLocal('speak_all_replies', v)}
                      ariaLabel="Speak typed replies too"
                    />
                  </SettingRow>
                  <SectionDivider />
                  <TtsStatusIndicator />
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <Select
                      label="Assistant Voice"
                      tooltip="Choose the voice Viola uses when speaking responses aloud."
                      value={localSettings.tts_voice || 'default'}
                      onChange={(v) => updateLocal('tts_voice', v)}
                      options={[
                        { value: 'default', label: 'Default' },
                        { value: 'alloy', label: 'Alloy' },
                        { value: 'echo', label: 'Echo' },
                        { value: 'nova', label: 'Nova' },
                      ]}
                    />
                  </div>
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <Slider
                      label="Speaking Speed"
                      tooltip="Controls how fast Viola speaks."
                      value={localSettings.tts_rate ?? 150}
                      onChange={(v) => updateLocal('tts_rate', v)}
                      min={100}
                      max={200}
                    />
                  </div>
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <Slider
                      label="Assistant Volume"
                      tooltip="Controls how loud spoken responses are."
                      value={localSettings.tts_volume ?? 80}
                      onChange={(v) => updateLocal('tts_volume', v)}
                    />
                  </div>
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <label
                      style={{
                        display: 'block',
                        marginBottom: '4px',
                        color: theme.colors.textSecondary,
                        fontSize: '14px',
                      }}
                    >
                      Your Name
                    </label>
                    <div
                      style={{
                        color: theme.colors.textMuted,
                        fontSize: '12px',
                        marginBottom: '8px',
                      }}
                    >
                      Optional. What Viola should call you and (if it's tricky) how to say it.
                    </div>
                    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                      <input
                        type="text"
                        placeholder="Name (e.g. Jihad)"
                        value={localSettings.user_name || ''}
                        onChange={(e) => updateLocal('user_name', e.target.value)}
                        style={{
                          flex: '1 1 140px',
                          minWidth: 0,
                          boxSizing: 'border-box',
                          padding: '12px 16px',
                          minHeight: '44px',
                          borderRadius: '12px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: theme.colors.bgCard,
                          color: theme.colors.textPrimary,
                          fontSize: '14px',
                          outline: 'none',
                        }}
                      />
                      <input
                        type="text"
                        placeholder={
                          localSettings.user_name ? 'How to say it (e.g. Jee hahd)' : 'Set a name first'
                        }
                        value={
                          (localSettings.tts_pronunciation_overrides || {})[
                            localSettings.user_name || ''
                          ] || ''
                        }
                        onChange={(e) => {
                          const name = (localSettings.user_name || '').trim();
                          if (!name) return;
                          const next = { ...(localSettings.tts_pronunciation_overrides || {}) };
                          const pron = e.target.value;
                          if (pron) {
                            next[name] = pron;
                          } else {
                            delete next[name];
                          }
                          updateLocal('tts_pronunciation_overrides', next);
                        }}
                        disabled={!localSettings.user_name}
                        style={{
                          flex: '1 1 140px',
                          minWidth: 0,
                          boxSizing: 'border-box',
                          padding: '12px 16px',
                          minHeight: '44px',
                          borderRadius: '12px',
                          border: `1px solid ${theme.colors.borderLight}`,
                          backgroundColor: theme.colors.bgCard,
                          color: theme.colors.textPrimary,
                          fontSize: '14px',
                          outline: 'none',
                          opacity: localSettings.user_name ? 1 : 0.5,
                        }}
                      />
                    </div>
                  </div>
                </>
              )}
            </Section>

            <AdvancedSection title="Voice Recognition & Advanced">
              <div style={{ padding: '16px 20px' }}>
                <Select
                  label="Voice Recognition"
                  tooltip="Choose whether Viola turns your speech into text on this device or keeps voice recognition off."
                  value={sttEngineValue}
                  onChange={(v) => updateLocal('stt_engine', v)}
                  options={[
                    { value: 'whisper_local', label: 'On-device (Fast, Private)' },
                    { value: 'whisper_api', label: 'Cloud (OpenAI Whisper)' },
                    { value: 'none', label: 'Off' },
                  ]}
                />
                <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '8px' }}>
                  Cloud transcription also requires Cloud Speech-to-Text consent and a configured OpenAI API key.
                </div>
              </div>
              {localSettings.stt_engine === 'whisper_local' || !localSettings.stt_engine ? (
                <>
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <Select
                      label="Accuracy Level"
                      tooltip="Choose the balance between faster responses and more accurate transcription."
                      value={localSettings.whisper_model || 'base'}
                      onChange={(v) => updateLocal('whisper_model', v)}
                      options={[
                        { value: 'tiny', label: 'Fast (Lower accuracy)' },
                        { value: 'base', label: 'Balanced' },
                        { value: 'small', label: 'Accurate' },
                        { value: 'medium', label: 'Most accurate (Slower)' },
                      ]}
                    />
                  </div>
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <Select
                      label="Processing Hardware"
                      tooltip="Choose whether speech recognition runs on your CPU or an NVIDIA GPU."
                      value={localSettings.whisper_device || 'cpu'}
                      onChange={(v) => updateLocal('whisper_device', v)}
                      options={[
                        { value: 'cpu', label: 'CPU (works everywhere)' },
                        { value: 'cuda', label: 'GPU - NVIDIA (faster)' },
                      ]}
                    />
                  </div>
                  <SectionDivider />
                  <div style={{ padding: '16px 20px' }}>
                    <Select
                      label="Voice Language"
                      tooltip="The language you speak to Viola. Auto-detect works for most users."
                      value={localSettings.whisper_language || 'auto'}
                      onChange={(v) => updateLocal('whisper_language', v)}
                      options={[
                        { value: 'auto', label: 'Auto-detect' },
                        { value: 'en', label: 'English' },
                        { value: 'es', label: 'Spanish' },
                        { value: 'fr', label: 'French' },
                        { value: 'de', label: 'German' },
                        { value: 'it', label: 'Italian' },
                        { value: 'pt', label: 'Portuguese' },
                        { value: 'nl', label: 'Dutch' },
                        { value: 'ru', label: 'Russian' },
                        { value: 'ja', label: 'Japanese' },
                        { value: 'zh', label: 'Chinese' },
                        { value: 'ko', label: 'Korean' },
                        { value: 'ar', label: 'Arabic' },
                      ]}
                    />
                  </div>
                </>
              ) : null}
            </AdvancedSection>
          </>
        );
      }

      case 'connections':
        return (
          <>
            <SmartHomeWizard
              settings={localSettings}
              onSettingChange={updateLocal}
              onSettingsChange={updateLocalSettings}
              autoLoadSettings={false}
            />

            <Section title="Calendar">
              <CalendarSettings />
              <SectionDivider />
              <ICloudCalendarSettings />
            </Section>

            <Section title="Telegram">
              <div style={{ padding: '12px 12px 0' }}>
                <QRPairCard channel="telegram" />
              </div>
              <div style={{ padding: '0 12px 12px' }}>
                <MessagingTab
                  settings={localSettings}
                  onSettingChange={updateLocal}
                />
              </div>
            </Section>

          </>
        );

      // ═══════════════════════════════════════════════════════════════
      case 'system':
        // #4226: every control on this tab is a setting for the INSTALLED
        // desktop app — the OS tray, start-on-boot, the local API port, the
        // machine's audio devices, the local updater, and the Advanced
        // Settings window's diagnostics/reset. A browser tab has no tray, no
        // boot hook and no local port, and none of the backing routes
        // (`/v1/settings/*`, `/v1/diagnostics/*`) are served on cloud, so
        // every one of these was a live control that silently did nothing.
        if (isFeatureHidden('system_controls')) {
          return (
            <Section title="Desktop settings">
              <div style={{ padding: '16px 20px' }}>
                <DesktopUpsell feature="system_controls" />
              </div>
            </Section>
          );
        }
        return (
          <>
            <Section title="Audio Devices">
              <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <Select
                  label="Microphone"
                  tooltip="Choose the microphone Viola listens to."
                  value={String(localSettings.input_device ?? '')}
                  onChange={(v) => updateLocal('input_device', v)}
                  options={inputDeviceOptions}
                />
                <Select
                  label="Speaker"
                  tooltip="Choose the speaker Viola uses for playback and speech."
                  value={String(localSettings.output_device ?? '')}
                  onChange={(v) => updateLocal('output_device', v)}
                  options={outputDeviceOptions}
                />
                <button
                  type="button"
                  onClick={async () => {
                    setIsRefreshingDevices(true);
                    setDevicesRefreshError(null);
                    try {
                      await refreshDevices();
                    } catch (err) {
                      setDevicesRefreshError(getSafeErrorMessage(err, 'Could not refresh audio devices.'));
                    } finally {
                      setIsRefreshingDevices(false);
                    }
                  }}
                  disabled={isRefreshingDevices}
                  style={{
                    alignSelf: 'flex-start',
                    padding: '8px 14px',
                    minHeight: '44px',
                    borderRadius: '8px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: 'transparent',
                    color: theme.colors.textSecondary,
                    fontSize: '13px',
                    cursor: isRefreshingDevices ? 'default' : 'pointer',
                    opacity: isRefreshingDevices ? 0.6 : 1,
                  }}
                >
                  {isRefreshingDevices ? 'Refreshing...' : 'Refresh devices'}
                </button>
                {devicesRefreshError && (
                  <div style={{ color: theme.colors.statusRed, fontSize: '12px' }}>
                    {devicesRefreshError}
                  </div>
                )}
              </div>
            </Section>

            <Section title="Notifications">
              <SettingRow title="Desktop Notifications" description="Show system notifications for events">
                <Toggle
                  checked={localSettings.show_notifications ?? true}
                  onChange={async (v) => {
                    if (v) {
                      const { requestNotificationPermission } = await import('../utils/notifications');
                      await requestNotificationPermission();
                    }
                    updateLocal('show_notifications', v);
                  }}
                  ariaLabel="Enable desktop notifications"
                />
              </SettingRow>
            </Section>

            <Section title="Window">
              <SettingRow title="Minimize to System Tray" description="Keep running in background when closed">
                <Toggle
                  checked={localSettings.minimize_to_tray ?? true}
                  onChange={(v) => updateLocal('minimize_to_tray', v)}
                  ariaLabel="Minimize to system tray"
                />
              </SettingRow>
              <SectionDivider />
              <SettingRow title={startupToggleCopy.title} description={startupToggleCopy.description}>
                <Toggle
                  checked={localSettings.start_on_boot ?? false}
                  onChange={(v) => updateLocal('start_on_boot', v)}
                  ariaLabel={startupToggleCopy.title}
                />
              </SettingRow>
            </Section>

            <Section title="Updates">
              <SettingRow
                title="Check for New Versions"
                description="Periodically check useviola.com for a newer version and notify you. A minimal support-floor check still runs so Viola can warn you if your version is no longer safe to keep running."
              >
                <Toggle
                  checked={localSettings.auto_update_check_enabled ?? true}
                  onChange={(v) => updateLocal('auto_update_check_enabled', v)}
                  ariaLabel="Check for new versions"
                />
              </SettingRow>
            </Section>

            <AdvancedSection title="Advanced">
              <SettingRow
                title="Network API Port"
                description="The HTTP port the local Viola server binds to. Restart required to take effect."
              >
                <input
                  type="number"
                  value={localSettings.api_port || 8756}
                  onChange={(e) => updateLocal('api_port', Number(e.target.value))}
                  min={1}
                  max={65535}
                  style={{
                    width: '120px',
                    padding: '12px 16px',
                    minHeight: '44px',
                    borderRadius: '12px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.bgCard,
                    color: theme.colors.textPrimary,
                    fontSize: '14px',
                    outline: 'none',
                  }}
                />
              </SettingRow>
            </AdvancedSection>

            <div style={{ marginTop: '20px' }}>
              <FooterButton
                variant="secondary"
                onClick={handleOpenAdvancedSettings}
              >
                Open Advanced Settings
              </FooterButton>
            </div>
          </>
        );

      default:
        return null;
    }
  };

  const activeTabContent = renderTabContent(activeTab);

  // Note: isOpen check removed — parent conditionally mounts via {settingsOpen && <SettingsModal/>}

  const isMobileViewport = typeof window !== 'undefined' && window.innerWidth < 480;
  const isNarrowSettings = isMobileViewport || modalWidth < 720;
  const modalOverlayPadding = isMobileViewport
    ? 'env(safe-area-inset-top, 8px) 8px env(safe-area-inset-bottom, 8px)'
    : '20px';
  const modalMaxHeight = isMobileViewport
    ? 'min(95vh, calc(100dvh - env(safe-area-inset-top, 8px) - env(safe-area-inset-bottom, 8px) - 16px))'
    : 'min(85vh, calc(100dvh - 40px))';

  return (
    <>
      <ScrollbarStyles />

      {/* Modal Backdrop */}
      <div
        style={{
          position: 'fixed',
          inset: 0,
          backgroundColor: theme.colors.overlay,
          // backdropFilter removed: --disable-gpu in viola_qt.py forces CPU rendering;
          // blur(8px) on a fixed full-screen div is a full-frame CPU Gaussian blur on
          // every tab switch paint — measured cause of Settings slowness in Qt WebView.
          zIndex: 1000,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: modalOverlayPadding,
          boxSizing: 'border-box',
          fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",
        }}
        onClick={handleCancel}
      >
        {/* Settings Modal */}
        <div
          ref={modalRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="settings-modal-title"
          data-testid="settings-modal"
          style={{
            position: 'relative',
            width: '100%',
            maxWidth: isNarrowSettings ? '720px' : '960px',
            maxHeight: modalMaxHeight,
            backgroundColor: theme.colors.bgCard,
            borderRadius: isMobileViewport ? '16px' : '24px',
            boxShadow: `0 24px 80px ${theme.colors.shadowDeep}, 0 0 60px rgba(107,46,27,0.10), inset 0 0 0 1px ${theme.colors.bronzeHairline}`,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: isMobileViewport ? '16px' : '20px 24px',
            borderBottom: `1px solid ${theme.colors.bronzeHairline}`,
            flexShrink: 0,
          }}>
            <h2 id="settings-modal-title" style={{ margin: 0, fontFamily: theme.fonts.display, fontSize: '25px', fontWeight: 600, letterSpacing: '0.3px', color: theme.colors.textBright }}>
              Settings
            </h2>
            <CloseButton onClick={handleCancel} />
          </div>

          <div
            style={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              flexDirection: isNarrowSettings ? 'column' : 'row',
            }}
          >
            {!isNarrowSettings && (
              <aside
                aria-label="Settings sections"
                style={{
                  width: '220px',
                  flexShrink: 0,
                  borderRight: `1px solid ${theme.colors.borderSubtle}`,
                  padding: '16px 12px',
                  boxSizing: 'border-box',
                  backgroundColor: theme.colors.bgSurface,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '10px',
                }}
              >
                <input
                  type="search"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder="Search settings"
                  aria-label="Search settings"
                  style={{
                    width: '100%',
                    minHeight: '44px',
                    padding: '10px 12px',
                    borderRadius: '10px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.bgCard,
                    color: theme.colors.textPrimary,
                    fontSize: '13px',
                    outline: 'none',
                    boxSizing: 'border-box',
                  }}
                />
                <div className="viola-scrollbar" style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  {visibleTabs.map((tab) => {
                    const Icon = tab.icon;
                    const isActive = activeTab === tab.id;
                    return (
                      <button
                        key={tab.id}
                        type="button"
                        onClick={tabClickHandlers[tab.id]}
                        aria-current={isActive ? 'page' : undefined}
                        style={{
                          width: '100%',
                          minHeight: '44px',
                          boxSizing: 'border-box',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '10px',
                          padding: '10px 12px',
                          borderRadius: '8px',
                          border: `1px solid ${isActive ? theme.colors.accentBorder : 'transparent'}`,
                          backgroundColor: isActive ? theme.colors.accentSubtle : 'transparent',
                          color: isActive ? theme.colors.accent : theme.colors.textSecondary,
                          fontSize: '14px',
                          fontWeight: isActive ? 600 : 500,
                          textAlign: 'left',
                          cursor: 'pointer',
                        }}
                      >
                        <Icon />
                        <span>{tab.label}</span>
                      </button>
                    );
                  })}
                  {visibleTabs.length === 0 && (
                    <div style={{ color: theme.colors.textMuted, fontSize: '13px', padding: '10px 12px' }}>
                      No matching sections.
                    </div>
                  )}
                </div>
              </aside>
            )}

            <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              {isNarrowSettings && (
                <div
                  style={{
                    padding: '12px',
                    borderBottom: `1px solid ${theme.colors.borderSubtle}`,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '10px',
                    flexShrink: 0,
                  }}
                >
                  <input
                    type="search"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    placeholder="Search settings"
                    aria-label="Search settings"
                    style={{
                      width: '100%',
                      minHeight: '44px',
                      padding: '10px 12px',
                      borderRadius: '10px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgCard,
                      color: theme.colors.textPrimary,
                      fontSize: '13px',
                      outline: 'none',
                      boxSizing: 'border-box',
                    }}
                  />
                  <select
                    value={activeTab}
                    onChange={(e) => setActiveTab(e.target.value)}
                    aria-label="Settings section"
                    disabled={visibleTabs.length === 0}
                    style={{
                      width: '100%',
                      minHeight: '44px',
                      padding: '10px 12px',
                      borderRadius: '10px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgElevated,
                      color: theme.colors.textPrimary,
                      fontSize: '14px',
                      outline: 'none',
                      boxSizing: 'border-box',
                    }}
                  >
                    {visibleTabs.map((tab) => (
                      <option key={tab.id} value={tab.id}>{tab.label}</option>
                    ))}
                  </select>
                </div>
              )}

              <div
                role="tabpanel"
                aria-label={`${activeTab} settings`}
                className="viola-scrollbar"
                style={{
                  flex: 1,
                  overflowY: 'auto',
                  WebkitOverflowScrolling: 'touch',
                  padding: isMobileViewport ? '16px 12px' : '24px',
                  display: 'flex',
                  flexDirection: 'column',
                }}
              >
                {/* The footer (Cancel / Save) scrolls with the tab content and
                    sticks to the bottom of the scroll viewport only when the
                    content is short (flex:1 0 auto spacer). A footer pinned as
                    a fixed sibling OVER an internally-scrolling panel
                    geometrically overlaps whatever row sits at the scroll fold
                    (raw-rect overlap the display-integrity gate flags); this
                    keeps every button identical while removing that overlap. */}
                <div style={{ flex: '1 0 auto' }}>
                  {loading ? (
                    <div style={{ textAlign: 'center', color: theme.colors.textMuted, padding: '40px' }}>
                      Loading settings...
                    </div>
                  ) : visibleTabs.length === 0 ? (
                    <div style={{ textAlign: 'center', color: theme.colors.textMuted, padding: '40px' }}>
                      No settings match "{debouncedSearchTerm}".
                    </div>
                  ) : (
                    activeTabContent
                  )}
                </div>

                {/* Footer (scrolls with content) */}
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'flex-end',
                  gap: '12px',
                  marginTop: isMobileViewport ? '16px' : '24px',
                  paddingTop: isMobileViewport ? '14px' : '16px',
                  borderTop: `1px solid ${theme.colors.borderLight}`,
                  flexShrink: 0,
                  flexWrap: 'wrap',
                }}>
                  {error && (
                    <span style={{ color: theme.colors.statusRed, fontSize: '13px', marginRight: 'auto' }}>
                      {getSafeErrorMessage(error, 'Something went wrong. Check your connection and try again.')}
                    </span>
                  )}
                  <FooterButton variant="secondary" onClick={handleCancel}>
                    Cancel
                  </FooterButton>
                  <FooterButton
                    variant="primary"
                    onClick={handleSave}
                    disabled={!hasChanges || saving}
                  >
                    {saving ? (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
                        <svg width="12" height="12" viewBox="0 0 24 24" data-essential-motion="spin" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite', flexShrink: 0 }}>
                          <path d="M21 12a9 9 0 11-6.219-8.56" />
                        </svg>
                        Saving...
                      </span>
                    ) : 'Save Changes'}
                  </FooterButton>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      {paymentCardDialog && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={paymentCardDialog.mode === 'edit' ? 'Edit payment method' : 'Add payment method'}
          style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: theme.colors.overlay,
            zIndex: 1100,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '20px',
            boxSizing: 'border-box',
          }}
          onClick={closePaymentCardDialog}
        >
          <div
            style={{
              width: 'min(520px, 100%)',
              backgroundColor: theme.colors.bgCard,
              borderRadius: '16px',
              border: `1px solid ${theme.colors.borderLight}`,
              boxShadow: `0 24px 80px ${theme.colors.shadowDeep}`,
              overflow: 'hidden',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ padding: '18px 20px', borderBottom: `1px solid ${theme.colors.borderSubtle}` }}>
              <div style={{ color: theme.colors.textPrimary, fontSize: '16px', fontWeight: 600 }}>
                {paymentCardDialog.mode === 'edit' ? 'Edit payment method' : 'Add payment method'}
              </div>
              {paymentCardDialog.mode === 'add' && (
                <div style={{ marginTop: '8px', color: theme.colors.textSecondary, fontSize: '13px', lineHeight: 1.45 }}>
                  For agent purchases we recommend a virtual card from your bank or Privacy.com — set a spend limit and lock it to a merchant. If you store a regular card, it is protected on your device but you are responsible for your device's security.
                </div>
              )}
            </div>
            <div style={{ padding: '18px 20px', display: 'grid', gridTemplateColumns: '1fr 120px', gap: '14px' }}>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                  Card label
                </label>
                <input
                  type="text"
                  value={paymentCardForm.label}
                  onChange={(e) => updatePaymentCardForm('label', e.target.value)}
                  style={{
                    width: '100%',
                    padding: '10px 12px',
                    minHeight: '44px',
                    borderRadius: '8px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.bgElevated,
                    color: theme.colors.textPrimary,
                    boxSizing: 'border-box',
                  }}
                />
              </div>
              {paymentCardDialog.mode === 'add' && (
                <>
                  <div>
                    <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                      Card number
                    </label>
                    <input
                      type="text"
                      inputMode="numeric"
                      autoComplete="off"
                      maxLength={23}
                      value={paymentCardForm.number}
                      onChange={(e) => updatePaymentCardForm('number', e.target.value.replace(/[^\d\s-]/g, '').slice(0, 23))}
                      style={{
                        width: '100%',
                        padding: '10px 12px',
                        minHeight: '44px',
                        borderRadius: '8px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: theme.colors.bgElevated,
                        color: theme.colors.textPrimary,
                        boxSizing: 'border-box',
                      }}
                    />
                  </div>
                  <div>
                    <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                      CVC
                    </label>
                    <input
                      type="password"
                      inputMode="numeric"
                      autoComplete="off"
                      maxLength={4}
                      value={paymentCardForm.cvc}
                      onChange={(e) => updatePaymentCardForm('cvc', e.target.value.replace(/\D/g, '').slice(0, 4))}
                      style={{
                        width: '100%',
                        padding: '10px 12px',
                        minHeight: '44px',
                        borderRadius: '8px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: theme.colors.bgElevated,
                        color: theme.colors.textPrimary,
                        boxSizing: 'border-box',
                      }}
                    />
                  </div>
                </>
              )}
              <div>
                <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                  Holder name
                </label>
                <input
                  type="text"
                  value={paymentCardForm.holder_name}
                  onChange={(e) => updatePaymentCardForm('holder_name', e.target.value)}
                  style={{
                    width: '100%',
                    padding: '10px 12px',
                    minHeight: '44px',
                    borderRadius: '8px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.bgElevated,
                    color: theme.colors.textPrimary,
                    boxSizing: 'border-box',
                  }}
                />
              </div>
              {paymentCardDialog.mode === 'edit' && (
                <div>
                  <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                    Last 4
                  </label>
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={4}
                    value={paymentCardForm.last4}
                    onChange={(e) => updatePaymentCardForm('last4', e.target.value.replace(/\D/g, '').slice(0, 4))}
                    style={{
                      width: '100%',
                      padding: '10px 12px',
                      minHeight: '44px',
                      borderRadius: '8px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgElevated,
                      color: theme.colors.textPrimary,
                      boxSizing: 'border-box',
                    }}
                  />
                </div>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                <div>
                  <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                    Month
                  </label>
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={2}
                    value={paymentCardForm.exp_month}
                    onChange={(e) => updatePaymentCardForm('exp_month', e.target.value.replace(/\D/g, '').slice(0, 2))}
                    style={{
                      width: '100%',
                      padding: '10px 12px',
                      minHeight: '44px',
                      borderRadius: '8px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgElevated,
                      color: theme.colors.textPrimary,
                      boxSizing: 'border-box',
                    }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                    Year
                  </label>
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={4}
                    value={paymentCardForm.exp_year}
                    onChange={(e) => updatePaymentCardForm('exp_year', e.target.value.replace(/\D/g, '').slice(0, 4))}
                    style={{
                      width: '100%',
                      padding: '10px 12px',
                      minHeight: '44px',
                      borderRadius: '8px',
                      border: `1px solid ${theme.colors.borderLight}`,
                      backgroundColor: theme.colors.bgElevated,
                      color: theme.colors.textPrimary,
                      boxSizing: 'border-box',
                    }}
                  />
                </div>
              </div>
              <div>
                <label style={{ display: 'block', marginBottom: '6px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                  Auth ceiling
                </label>
                <input
                  type="number"
                  min="0"
                  value={paymentCardForm.auth_ceiling_dollars}
                  onChange={(e) => updatePaymentCardForm('auth_ceiling_dollars', e.target.value)}
                  style={{
                    width: '100%',
                    padding: '10px 12px',
                    minHeight: '44px',
                    borderRadius: '8px',
                    border: `1px solid ${theme.colors.borderLight}`,
                    backgroundColor: theme.colors.bgElevated,
                    color: theme.colors.textPrimary,
                    boxSizing: 'border-box',
                  }}
                />
              </div>
              {paymentCardActionError && (
                <div style={{ gridColumn: '1 / -1', color: theme.colors.statusRed, fontSize: '13px' }}>
                  {paymentCardActionError}
                </div>
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', padding: '14px 20px', borderTop: `1px solid ${theme.colors.borderSubtle}` }}>
              <FooterButton variant="secondary" onClick={closePaymentCardDialog} disabled={paymentCardSaving}>
                Cancel
              </FooterButton>
              <FooterButton variant="primary" onClick={handleSavePaymentCard} disabled={paymentCardSaving}>
                {paymentCardSaving ? 'Saving...' : 'Save'}
              </FooterButton>
            </div>
          </div>
        </div>
      )}
      <AdvancedSettingsWindow
        isOpen={showAdvancedSettings}
        onClose={() => setShowAdvancedSettings(false)}
        settings={localSettings}
        onSettingChange={updateLocal}
        onSettingsChange={updateLocalSettings}
      />
    </>
  );
});

SettingsModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  initialTab: PropTypes.string,
  initialSection: PropTypes.string,
};

export default SettingsModal;
