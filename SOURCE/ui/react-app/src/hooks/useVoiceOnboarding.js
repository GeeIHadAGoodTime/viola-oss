import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { apiFetch } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';
import { getMicPermissionGuidance } from '../utils/platform';
import { gotrueClient } from '../lib/gotrue_client';
import '../i18n';

// First-run onboarding (account sign-in, local-TTS narration, mic-permission
// checks) is desktop-hub shaped. On the cloud web build the visitor signs in
// through the GoTrue account front door (main.jsx) instead. A hub-backed
// multiroom spoke follows the hub's real onboarding state rather than being
// force-hidden by surface type.
//
// The account phase watches the shared GoTrue session (the desktop proxies
// /auth/v1 to the cloud) and auto-advances once the user signs in through
// Account settings. The old device-pair code dance was removed with Path A
// cloud-proxy login (6a590210); onboarding must never call /auth/pair/*.

const SIGNIN_POLL_INTERVAL_MS = 2500;

// v2 lean flow — the fewest decisions, fastest path to value. Telemetry was
// removed from onboarding entirely (it defaults OFF and lives in
// Settings → Privacy & Data). The old mic-check and try-a-command steps are
// merged into one "hold PTT and say the phrase" moment that both proves the
// mic works AND delivers the first-answer aha. Attribution is a single,
// clearly-optional tap kept last so it never taxes the path to value.
//
// Autonomy tier (solo | ensemble | symphony) — the agent's permission/trust
// level — is surfaced as its own step and written through the same settings
// path the SettingsModal uses.
const TIER_BROWSER_MODE = {
  solo: 'ephemeral',
  ensemble: 'viola',
  symphony: 'viola',
};
const AUTONOMY_TIERS = ['solo', 'ensemble', 'symphony'];

// No phase is a wall. First run is a guided setup, and every step it covers
// has a safe default that is reachable again from Settings (agent_autonomy
// defaults to the most restricted tier, "solo"), so the dismiss control is
// live on every phase rather than on the last one only. The old per-phase
// canSkip flag is gone: it was false everywhere except hear_about, which
// meant a user who could not finish a step had no way out of first run at all
// (#4225).
const PHASES = {
  welcome: {
    id: 'welcome',
    index: 0,
    speechKey: 'onboarding.phases.welcome.speech',
    highlight: null,
    waitFor: 'welcome_continue',
  },
  account_pair: {
    id: 'account_pair',
    index: 1,
    speechKey: 'onboarding.phases.account_pair.speech',
    highlight: null,
    waitFor: 'account_signin',
  },
  cloud_consent: {
    id: 'cloud_consent',
    index: 2,
    speechKey: 'onboarding.phases.cloud_consent.speech',
    highlight: null,
    waitFor: 'cloud_consent',
  },
  autonomy_tier: {
    id: 'autonomy_tier',
    index: 3,
    speechKey: 'onboarding.phases.autonomy_tier.speech',
    highlight: null,
    waitFor: 'autonomy',
  },
  mic_try: {
    id: 'mic_try',
    index: 4,
    speechKey: 'onboarding.phases.mic_try.speech',
    highlight: 'ptt-button',
    waitFor: 'command',
    suggestions: [
      {
        textKey: 'onboarding.phases.mic_try.suggestions.two_plus_two.text',
        labelKey: 'onboarding.phases.mic_try.suggestions.two_plus_two.label',
      },
    ],
  },
  hear_about: {
    id: 'hear_about',
    index: 5,
    speechKey: 'onboarding.phases.hear_about.speech',
    highlight: null,
    waitFor: 'attribution',
  },
};

const PHASE_ORDER = ['welcome', 'account_pair', 'cloud_consent', 'autonomy_tier', 'mic_try', 'hear_about'];
const TOTAL_PHASES = PHASE_ORDER.length;

function speakText(text) {
  let cancelled = false;
  // Lets the server-TTS wait below race a cancel() call, the same way the
  // fallback branch's speechSynthesis wait already can (via settle()).
  let onCancel = null;

  const promise = (async () => {
    if (cancelled) return;

    try {
      // Viola's own voice, when this build has one. The desktop registers
      // /v1/tts/speak; the cloud web build deliberately does not, so there the
      // call 404s (apiFetch throws) and narration falls through to the
      // browser voice below. `spoken` is the server telling us it actually
      // accepted the line — a muted or engine-less desktop answers
      // spoken:false, and narrating to nobody is exactly what we must not do.
      const result = await apiFetch('/v1/tts/speak', {
        method: 'POST',
        body: JSON.stringify({ text }),
      });
      if (result?.spoken === true && !cancelled) {
        const wordCount = text.split(/\s+/).length;
        const estimatedMs = Math.max(1800, wordCount * 230);
        // Race the estimated-duration wait against cancel() so a concurrent
        // phase transition (clearTimers()) can actually cut this short
        // instead of leaving it to resolve on its own and fire a stale
        // advancePhase() later (possible double-advance).
        await new Promise((resolve) => {
          const timer = setTimeout(resolve, estimatedMs);
          onCancel = () => {
            clearTimeout(timer);
            resolve();
          };
        });
        onCancel = null;
        return;
      }
    } catch {
      // Fall back to browser speech synthesis.
    }

    if (cancelled) return;

    // Fallback narration path. This must NEVER be able to hang: onboarding
    // advances when this promise resolves, so if browser speech synthesis is
    // unavailable or never fires onend (e.g. a headless/voice-less WebView, or
    // TTS assets missing), a hard time cap still resolves it. Otherwise a
    // missing TTS voice could strand the user on a step forever.
    const wordCount = text.split(/\s+/).length;
    const capMs = Math.max(1800, wordCount * 230) + 1500;

    return new Promise(resolve => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      const timer = setTimeout(finish, capMs);
      const settle = () => { clearTimeout(timer); finish(); };

      if (cancelled || typeof window === 'undefined' || !('speechSynthesis' in window)) {
        settle();
        return;
      }
      try {
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.rate = 0.95;
        utterance.pitch = 1.0;
        const voices = window.speechSynthesis.getVoices();
        const preferred = voices.find(v => /samantha|zira|female|google.*us/i.test(v.name));
        if (preferred) utterance.voice = preferred;
        utterance.onend = settle;
        utterance.onerror = settle;
        window.speechSynthesis.speak(utterance);
      } catch {
        settle();
      }
    });
  })();

  return {
    promise,
    cancel: () => {
      cancelled = true;
      if (onCancel) onCancel();
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    },
  };
}

async function checkOnboardingStatus() {
  try {
    const data = await apiFetch('/v1/onboarding/status');
    return data?.completed === true;
  } catch {
    return false;
  }
}

async function completeOnboarding() {
  try {
    await apiFetch('/v1/onboarding/complete', { method: 'POST' });
  } catch {
    // Best effort; the user can still use the app.
  }
}

async function saveOnboardingStep(stepId, data) {
  try {
    await apiFetch('/v1/onboarding/save', {
      method: 'POST',
      body: JSON.stringify({ step_id: stepId, data }),
    });
  } catch {
    // Best effort.
  }
}

async function updateSettings(settings) {
  return apiFetch('/v1/settings', {
    method: 'POST',
    body: JSON.stringify({ settings }),
  });
}

// How long the capture probe listens before deciding a stream is digitally
// silent. Long enough that a device still ramping up is not misjudged, short
// enough that first run does not visibly stall.
const MIC_PROBE_LISTEN_MS = 700;
const MIC_PROBE_FRAME_MS = 50;

/**
 * Probe the stack that ACTUALLY captures the user's voice.
 *
 * Push-to-talk records through Chromium's getUserMedia inside QtWebEngine
 * (see useVoice.js), so that is the only thing whose verdict means anything.
 * The old gate asked a PyAudio stream on the Python side instead, which is a
 * different device enumeration with different rate negotiation: it blocked
 * first run on microphones Chromium opens happily, and — because its only
 * denial test was a zero-length read — waved through a privacy-denied
 * microphone that returns full buffers of digital silence.
 *
 * Constraints here are deliberately identical to useVoice.js's, so a probe
 * that passes means the real recorder will get a stream too.
 */
async function probeMicrophoneCapture() {
  if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    return { ok: false, reason: 'unsupported' };
  }

  let stream = null;
  let audioCtx = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 16000,
        echoCancellation: false,
        noiseSuppression: true,
      },
    });
  } catch (err) {
    const name = err?.name || '';
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      return { ok: false, reason: 'denied', errorName: name };
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return { ok: false, reason: 'no_device', errorName: name };
    }
    return { ok: false, reason: 'error', errorName: name };
  }

  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
      // No way to inspect the samples. The stream opened, which is already
      // more than the old check could prove; treat that as good enough rather
      // than inventing a denial.
      return { ok: true, silenceChecked: false };
    }
    audioCtx = new Ctx();
    const source = audioCtx.createMediaStreamSource(stream);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);

    // A microphone denied at the OS privacy layer still yields a live track;
    // it just carries pure digital zeroes. Real capture always carries a noise
    // floor, so a single non-zero sample proves audio is genuinely flowing.
    const deadline = Date.now() + MIC_PROBE_LISTEN_MS;
    while (Date.now() < deadline) {
      analyser.getFloatTimeDomainData(buf);
      for (let i = 0; i < buf.length; i += 1) {
        if (buf[i] !== 0) return { ok: true, silenceChecked: true };
      }
      await new Promise((resolve) => setTimeout(resolve, MIC_PROBE_FRAME_MS));
    }
    return { ok: false, reason: 'silent', silenceChecked: true };
  } catch {
    // The stream opened; only the inspection failed. Do not manufacture a
    // denial out of an analysis error.
    return { ok: true, silenceChecked: false };
  } finally {
    try {
      stream?.getTracks().forEach((track) => track.stop());
    } catch {
      // Nothing actionable — the probe is over either way.
    }
    try {
      await audioCtx?.close();
    } catch {
      // Same.
    }
  }
}

// Platform-appropriate guidance for a capture failure the probe already saw.
// This endpoint is advisory only now: it inspects the desktop audio stack,
// which is NOT the stack that records, so it must never be what decides
// whether the user may continue.
async function fetchMicGuidance(messages = {}) {
  try {
    const data = await apiFetch('/v1/onboarding/check-mic-permission');
    return (
      data?.error_message
      || messages.unknown
      || getMicPermissionGuidance()
    );
  } catch {
    // apiFetch threw (network/auth failure) before the backend could return
    // any platform-specific error_message — fall back to guidance for the
    // actual host OS (C-078: this used to hardcode Windows copy, which shipped
    // wrong instructions to Mac and Linux users on desktop).
    return getMicPermissionGuidance();
  }
}

async function checkSignedIn() {
  try {
    const { data, error } = await gotrueClient.getSession();
    if (error) return false;
    return Boolean(data && data.session);
  } catch {
    return false;
  }
}

export function useVoiceOnboarding() {
  const { t } = useTranslation();
  const onboardingAvailable = isFeatureAvailable('onboarding');
  const [isOnboarding, setIsOnboarding] = useState(false);
  const [phaseId, setPhaseId] = useState('welcome');
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [micRetries, setMicRetries] = useState(0);
  const [micPermission, setMicPermission] = useState({
    checking: false,
    granted: false,
    blocked: false,
    message: null,
  });
  const [micPermissionAttempt, setMicPermissionAttempt] = useState(0);
  const [signInStatus, setSignInStatus] = useState('checking');
  // True once the user has chosen to finish first run without a Viola account.
  // It is a UI-routing flag only: the account requirement itself lives in
  // core/account_gate.py and applies to Viola-managed AI, nothing else.
  const [accountSkipped, setAccountSkipped] = useState(false);
  const [paused, setPaused] = useState(false);
  const [cloudConsentStatus, setCloudConsentStatus] = useState('idle');
  const [cloudConsentError, setCloudConsentError] = useState(null);
  const [attributionStatus, setAttributionStatus] = useState('idle');
  const [autonomyStatus, setAutonomyStatus] = useState('idle');
  const [autonomyError, setAutonomyError] = useState(null);
  const [tryCommandStatus, setTryCommandStatus] = useState('idle');

  const skipRef = useRef(false);
  const timeoutRef = useRef(null);
  const speechRef = useRef(null);
  const phaseIdRef = useRef(phaseId);
  const pausedRef = useRef(false);
  const signInIntervalRef = useRef(null);
  const signInHandledRef = useRef(false);
  const mountedRef = useRef(true);
  const initStartedRef = useRef(false);
  const lastTryCommandRef = useRef(null);
  const commandFinishedRef = useRef(false);
  const cloudConsentStatusRef = useRef(cloudConsentStatus);
  const accountSkippedRef = useRef(false);

  useEffect(() => { phaseIdRef.current = phaseId; }, [phaseId]);
  useEffect(() => { cloudConsentStatusRef.current = cloudConsentStatus; }, [cloudConsentStatus]);
  useEffect(() => { accountSkippedRef.current = accountSkipped; }, [accountSkipped]);

  const phase = PHASES[phaseId] || PHASES.welcome;
  const phaseSpeech = t(phase.speechKey);
  const suggestions = useMemo(() => {
    if (!isOnboarding || phase.id !== 'mic_try' || tryCommandStatus === 'answered') {
      return null;
    }
    return (phase.suggestions || []).map((suggestion) => ({
      text: t(suggestion.textKey),
      label: t(suggestion.labelKey),
    }));
  }, [isOnboarding, phase, t, tryCommandStatus]);

  const clearTimers = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    if (speechRef.current) {
      speechRef.current.cancel();
      speechRef.current = null;
    }
  }, []);

  const stopSignInPolling = useCallback(() => {
    if (signInIntervalRef.current) {
      clearInterval(signInIntervalRef.current);
      signInIntervalRef.current = null;
    }
  }, []);

  const goToPhase = useCallback((nextId) => {
    if (skipRef.current) return;
    clearTimers();

    if (!PHASES[nextId]) {
      setIsOnboarding(false);
      completeOnboarding();
      return;
    }

    setPhaseId(nextId);
  }, [clearTimers]);

  const advancePhase = useCallback(() => {
    if (!onboardingAvailable) return;
    const currentIdx = PHASE_ORDER.indexOf(phaseIdRef.current);
    const nextIdx = currentIdx + 1;

    if (nextIdx >= TOTAL_PHASES) {
      setIsOnboarding(false);
      completeOnboarding();
    } else {
      goToPhase(PHASE_ORDER[nextIdx]);
    }
  }, [goToPhase, onboardingAvailable]);

  const handleSignedIn = useCallback(() => {
    if (signInHandledRef.current) return;
    signInHandledRef.current = true;
    stopSignInPolling();
    if (!mountedRef.current) return;
    setSignInStatus('signed_in');
    saveOnboardingStep('api_access', { account_signed_in: true });

    clearTimers();
    setIsSpeaking(true);
    const tts = speakText(t('onboarding.account_pair.signed_in_tts'));
    speechRef.current = tts;
    tts.promise.then(() => {
      setIsSpeaking(false);
      if (!skipRef.current) advancePhase();
    });
  }, [advancePhase, clearTimers, stopSignInPolling, t]);

  // The guest path out of the account step. Viola's runtime only requires an
  // account for Viola-MANAGED AI: core/account_gate.py exempts BYOK, Codex and
  // local because they cost Viola nothing per request, and the gate fires at
  // the managed-provider construction boundary, never at launch. So onboarding
  // must not be stricter than the product — a user who does not want a cloud
  // account continues from here and finishes setup on their own provider key
  // or a local model (#4225).
  const onContinueWithoutAccount = useCallback(() => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'account_pair') return false;
    // A session that landed while the panel was open wins: they do have an
    // account, and handleSignedIn is already driving the advance.
    if (signInHandledRef.current) return false;

    stopSignInPolling();
    accountSkippedRef.current = true;
    setAccountSkipped(true);
    setSignInStatus('no_account');
    saveOnboardingStep('api_access', {
      account_signed_in: false,
      continued_without_account: true,
    });

    clearTimers();
    setIsSpeaking(true);
    const tts = speakText(t('onboarding.account_pair.no_account_tts'));
    speechRef.current = tts;
    tts.promise.then(() => {
      setIsSpeaking(false);
      if (!skipRef.current) advancePhase();
    });
    return true;
  }, [advancePhase, clearTimers, isOnboarding, onboardingAvailable, stopSignInPolling, t]);

  // The reverse of the guest path, offered on the AI step so the no-account
  // choice is never a one-way door inside first run.
  const onReturnToSignIn = useCallback(() => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'cloud_consent') return false;
    if (!accountSkippedRef.current) return false;

    accountSkippedRef.current = false;
    setAccountSkipped(false);
    setSignInStatus('checking');
    setCloudConsentStatus('idle');
    setCloudConsentError(null);
    goToPhase('account_pair');
    return true;
  }, [goToPhase, isOnboarding, onboardingAvailable]);

  // Account phase: watch the shared GoTrue session (signed in via Account
  // settings) and advance the moment it exists. Polling the session store is
  // deliberate — sign-in happens in a different component tree (SettingsModal
  // -> AccountTab), and the stored session is the one source of truth.
  useEffect(() => {
    if (!isOnboarding || phaseId !== 'account_pair') return undefined;

    let cancelled = false;
    const probe = async () => {
      const signedIn = await checkSignedIn();
      if (cancelled || !mountedRef.current) return;
      if (signedIn) {
        handleSignedIn();
      } else {
        setSignInStatus((prev) => (prev === 'signed_in' ? prev : 'awaiting_user'));
      }
    };

    probe();
    signInIntervalRef.current = setInterval(probe, SIGNIN_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      stopSignInPolling();
    };
  }, [handleSignedIn, isOnboarding, phaseId, stopSignInPolling]);

  useEffect(() => {
    if (!isOnboarding || skipRef.current || pausedRef.current) return undefined;

    const currentPhase = PHASES[phaseId];
    if (!currentPhase) return undefined;

    let cancelled = false;

    const runPhase = async () => {
      clearTimers();

      if (currentPhase.id === 'mic_try') {
        setMicPermission({
          checking: true,
          granted: false,
          blocked: false,
          message: null,
        });
        // The verdict comes from the real recorder, not from a proxy.
        const probe = await probeMicrophoneCapture();
        if (cancelled || skipRef.current || phaseIdRef.current !== 'mic_try') return;
        if (probe.ok) {
          setMicPermission({
            checking: false,
            granted: true,
            blocked: false,
            message: null,
          });
        } else {
          // Only now is the backend worth asking, and only for wording.
          const message = await fetchMicGuidance({ unknown: t('onboarding.mic.unknown') });
          if (cancelled || skipRef.current || phaseIdRef.current !== 'mic_try') return;
          setMicPermission({
            checking: false,
            granted: false,
            blocked: true,
            message,
          });
          return;
        }
      } else if (currentPhase.id === 'cloud_consent' && cloudConsentStatusRef.current === 'declined') {
        return;
      } else {
        setMicPermission({
          checking: false,
          granted: false,
          blocked: false,
          message: null,
        });
      }

      setIsSpeaking(true);
      const tts = speakText(t(currentPhase.speechKey));
      speechRef.current = tts;

      tts.promise.then(() => {
        if (cancelled || skipRef.current) return;
        setIsSpeaking(false);
      });
    };

    runPhase();

    return () => {
      cancelled = true;
      clearTimers();
      setIsSpeaking(false);
    };
  }, [clearTimers, isOnboarding, micPermissionAttempt, paused, phaseId, t]);

  const retryMicPermission = useCallback(() => {
    if (phaseIdRef.current !== 'mic_try') return;
    setMicPermissionAttempt(value => value + 1);
  }, []);

  // A microphone the probe cannot hear must not end first run. The probe can
  // still be wrong in the user's favour (a device it cannot inspect but the
  // recorder can open), and a user who simply wants to sort audio out later
  // should still be able to finish setup. This moves on without recording a
  // pass the mic never earned.
  const onSkipMicStep = useCallback(() => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'mic_try') return false;
    clearTimers();
    saveOnboardingStep('microphone_test', { passed: false, skipped: true });
    advancePhase();
    return true;
  }, [advancePhase, clearTimers, isOnboarding, onboardingAvailable]);

  // Merged mic + first-command step: the raw transcript only drives the mic
  // half of the check (did push-to-talk hear anything?). When the mic heard a
  // phrase, that same phrase is executed as the user's first command, so
  // completion + advance is driven by onCommandExecuted below — not here — so
  // the user actually hears Viola's answer (the aha) before moving on.
  const onMicTestResult = useCallback((transcript) => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'mic_try') return false;

    if (!micPermission.granted) {
      retryMicPermission();
      return true;
    }

    if (transcript && transcript.trim()) {
      // Heard a phrase — the command pipeline runs it and onCommandExecuted
      // finishes the step. Nothing to do here but record the mic passed.
      saveOnboardingStep('microphone_test', { passed: true, sample_detected: true });
      return true;
    }

    // Empty transcript: the mic did not catch anything. Nudge a retry without
    // leaving the step.
    clearTimers();
    const retryCount = micRetries + 1;
    setMicRetries(retryCount);
    setIsSpeaking(true);
    const tts = speakText(t('onboarding.mic.retry_tts'));
    speechRef.current = tts;
    tts.promise.then(() => setIsSpeaking(false));
    return true;
  }, [clearTimers, isOnboarding, micPermission.granted, micRetries, onboardingAvailable, retryMicPermission, t]);

  const onCloudConsentChoice = useCallback(async (choice) => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'cloud_consent') return false;

    const enableCloud = choice === 'enable';
    // Viola-managed AI is metered per account, so it is not a real option for
    // someone who just chose to continue without one. The panel does not offer
    // it in that state; this keeps ai_source from drifting to a setting that
    // would only fail later at the account gate.
    if (enableCloud && accountSkippedRef.current) return false;

    clearTimers();
    setCloudConsentStatus('saving');
    setCloudConsentError(null);

    const nextSettings = enableCloud
      ? { ai_source: 'managed' }
      : { ai_source: 'byok' };

    try {
      await updateSettings(nextSettings);
      saveOnboardingStep('ai_setup', {
        cloud_llm_consent: enableCloud,
        ai_source: enableCloud ? 'managed' : 'byok',
      });

      if (enableCloud) {
        setCloudConsentStatus('enabled');
        setIsSpeaking(true);
        const tts = speakText(t('onboarding.cloud.enabled_tts'));
        speechRef.current = tts;
        tts.promise.then(() => {
          setIsSpeaking(false);
          if (!skipRef.current) advancePhase();
        });
      } else {
        setCloudConsentStatus('declined');
        setIsSpeaking(true);
        const tts = speakText(t('onboarding.cloud.declined_tts'));
        speechRef.current = tts;
        tts.promise.then(() => setIsSpeaking(false));
      }
      return true;
    } catch {
      setCloudConsentStatus('error');
      setCloudConsentError(t('onboarding.cloud.save_error'));
      return false;
    }
  }, [advancePhase, clearTimers, isOnboarding, onboardingAvailable, t]);

  const onByokSetupDone = useCallback(() => {
    if (!onboardingAvailable) return;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'cloud_consent') return;
    setCloudConsentStatus('byok_ready');
    setIsSpeaking(true);
    const tts = speakText(t('onboarding.cloud.byok_ready_tts'));
    speechRef.current = tts;
    tts.promise.then(() => {
      setIsSpeaking(false);
      if (!skipRef.current) advancePhase();
    });
  }, [advancePhase, isOnboarding, onboardingAvailable, t]);

  const onAttributionChoice = useCallback(async (value) => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'hear_about') return false;
    if (attributionStatus === 'saving') return false;

    clearTimers();
    setAttributionStatus('saving');

    // '' = "prefer not to say" — the stored default. Anything else must be
    // one of the closed ATTRIBUTION_OPTIONS values (enforced server-side too).
    const answer = typeof value === 'string' ? value : '';
    try {
      await updateSettings({ attribution_self_report: answer });
    } catch {
      // The answer is optional marketing self-report; a failed save must
      // never block first-run. Continue without it.
    }
    setAttributionStatus('idle');
    if (!skipRef.current) advancePhase();
    return true;
  }, [advancePhase, attributionStatus, clearTimers, isOnboarding, onboardingAvailable]);

  // Autonomy tier — the agent's trust/permission level. Written through the
  // exact same settings path the SettingsModal uses (agent_autonomy plus the
  // derived browser_session_mode), so onboarding and Settings stay one source
  // of truth. This is a genuine permission decision, so the step is required
  // (not skippable); the panel highlights a sensible default.
  const onAutonomyChoice = useCallback(async (tier) => {
    if (!onboardingAvailable) return false;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'autonomy_tier') return false;
    if (autonomyStatus === 'saving') return false;
    if (!AUTONOMY_TIERS.includes(tier)) return false;

    clearTimers();
    setAutonomyStatus('saving');
    setAutonomyError(null);

    try {
      await updateSettings({
        agent_autonomy: tier,
        browser_session_mode: TIER_BROWSER_MODE[tier],
      });
      saveOnboardingStep('autonomy_tier', { agent_autonomy: tier });
      setAutonomyStatus('idle');
      if (!skipRef.current) advancePhase();
      return true;
    } catch {
      setAutonomyStatus('error');
      setAutonomyError(t('onboarding.autonomy.save_error'));
      return false;
    }
  }, [advancePhase, autonomyStatus, clearTimers, isOnboarding, onboardingAvailable, t]);

  const finishMicTryStep = useCallback(() => {
    if (!onboardingAvailable) return;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'mic_try') return;
    if (commandFinishedRef.current) return;
    commandFinishedRef.current = true;
    clearTimers();
    setTryCommandStatus('answered');
    saveOnboardingStep('quick_tutorial', {
      first_command: lastTryCommandRef.current || 'voice_command',
      completed: true,
    });
    // Viola has already spoken its answer to the first command (the aha). Move
    // on to the single, optional attribution tap — the last step.
    if (!skipRef.current) advancePhase();
  }, [advancePhase, clearTimers, isOnboarding, onboardingAvailable]);

  const onCommandExecuted = useCallback(() => {
    finishMicTryStep();
  }, [finishMicTryStep]);

  const onSuggestionTap = useCallback(async (text, sendCommand) => {
    if (!onboardingAvailable) return;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'mic_try' || !sendCommand) return;

    clearTimers();
    lastTryCommandRef.current = text;
    setTryCommandStatus('running');
    try {
      await sendCommand(text);
      finishMicTryStep();
    } catch {
      setTryCommandStatus('error');
      setIsSpeaking(true);
      const tts = speakText(t('onboarding.mic_try.error_tts'));
      speechRef.current = tts;
      tts.promise.then(() => setIsSpeaking(false));
    }
  }, [clearTimers, finishMicTryStep, isOnboarding, onboardingAvailable, t]);

  const onWelcomeContinue = useCallback(() => {
    if (!onboardingAvailable) return;
    if (!isOnboarding || skipRef.current || phaseIdRef.current !== 'welcome') return;
    // The welcome phase is a warm opener with no persisted step; the user taps
    // "Begin setup" (or lets the greeting finish) and we move to account sign-in.
    clearTimers();
    setIsSpeaking(false);
    advancePhase();
  }, [advancePhase, clearTimers, isOnboarding, onboardingAvailable]);

  // Dismiss first run from any phase. This used to be gated on the phase's own
  // canSkip flag, which was true only on the last step, so a user who could not
  // complete an earlier one (no account, denied microphone) was held on that
  // step indefinitely (#4225).
  const skipOnboarding = useCallback(() => {
    if (!onboardingAvailable) return;
    skipRef.current = true;
    clearTimers();
    stopSignInPolling();
    setIsOnboarding(false);
    completeOnboarding();
  }, [clearTimers, onboardingAvailable, stopSignInPolling]);

  const pauseOnboarding = useCallback(() => {
    if (!isOnboarding) return;
    clearTimers();
    pausedRef.current = true;
    setPaused(true);
  }, [clearTimers, isOnboarding]);

  const resumeOnboarding = useCallback(() => {
    if (!isOnboarding || !pausedRef.current) return;
    pausedRef.current = false;
    setPaused(false);
    if (phaseIdRef.current === 'mic_try') {
      setMicPermissionAttempt(value => value + 1);
    }
  }, [isOnboarding]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopSignInPolling();
      clearTimers();
    };
  }, [clearTimers, stopSignInPolling]);

  useEffect(() => {
    if (initStartedRef.current) return undefined;

    // Cloud web build: device-pairing onboarding never runs here.
    if (!onboardingAvailable) return undefined;

    initStartedRef.current = true;

    let mounted = true;

    const init = async () => {
      const completed = await checkOnboardingStatus();
      if (!mounted) return;
      if (!completed && mounted) {
        timeoutRef.current = setTimeout(() => {
          if (!skipRef.current && mounted) {
            setIsOnboarding(true);
          }
        }, 800);
      }
    };

    init();

    return () => {
      mounted = false;
    };
  }, [onboardingAvailable]);

  return {
    isOnboarding,
    phase: phase.id,
    phaseIndex: phase.index,
    totalPhases: TOTAL_PHASES,
    phaseContent: phaseSpeech,
    isSpeaking,

    activeHighlight: isOnboarding && !paused ? phase.highlight : null,

    isWelcomePhase: isOnboarding && phase.id === 'welcome',
    isAccountPairPhase: isOnboarding && phase.id === 'account_pair',
    signInStatus,
    accountSkipped,
    isMicPermissionPhase: isOnboarding && phase.id === 'mic_try',
    micPermission,
    isCloudConsentPhase: isOnboarding && phase.id === 'cloud_consent',
    cloudConsentStatus,
    cloudConsentError,
    isAutonomyPhase: isOnboarding && phase.id === 'autonomy_tier',
    autonomyStatus,
    autonomyError,
    isAttributionPhase: isOnboarding && phase.id === 'hear_about',
    attributionStatus,
    isMicTryPhase: isOnboarding && phase.id === 'mic_try',
    suggestions,
    tryCommandStatus,

    voiceModeOptions: null,
    isMultiRoomPhase: false,
    isMessagingPhase: false,

    skipOnboarding,
    onWelcomeContinue,
    onContinueWithoutAccount,
    onReturnToSignIn,
    onMicTestResult,
    onCommandExecuted,
    onSuggestionTap,
    onMicPermissionRetry: retryMicPermission,
    onSkipMicStep,
    onCloudConsentChoice,
    onByokSetupDone,
    onAutonomyChoice,
    onAttributionChoice,
    pauseOnboarding,
    resumeOnboarding,
  };
}

export default useVoiceOnboarding;
