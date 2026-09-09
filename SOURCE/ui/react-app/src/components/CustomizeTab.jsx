import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME, applyTheme } from '../config';
import { authFetch } from '../hooks/useViolaApi';
import { isCloudSurface } from './auth/cloudSurface';
import AccentPicker from './AccentPicker';
import WakeWordSection from './WakeWordSection';
import {
  Section,
  SectionDivider,
  Select,
  DangerButton,
  FooterButton,
  Icons,
} from './settings';

const VISIT_FLAG_KEY = 'viola.customize.welcomeDismissed';

// ── WelcomeBanner ───────────────────────────────────────────────────────────
// Shows on first visit to the Customize tab. After dismissal it collapses to
// a slim one-line hint. State persists in localStorage so it does NOT fire on
// startup — only when the user is already here.

function WelcomeBanner({ theme }) {
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(VISIT_FLAG_KEY) === '1';
    } catch (_e) {
      return false;
    }
  });

  const dismiss = useCallback(() => {
    setDismissed(true);
    try { localStorage.setItem(VISIT_FLAG_KEY, '1'); } catch (_e) { /* ignore */ }
  }, []);

  if (dismissed) {
    return (
      <div
        role="note"
        style={{
          display: 'flex', alignItems: 'center', gap: '10px',
          margin: '0 0 20px',
          padding: '10px 16px',
          borderRadius: '10px',
          backgroundColor: theme.colors.bgElevated,
          border: `1px solid ${theme.colors.borderSubtle}`,
          color: theme.colors.textMuted,
          fontSize: '12.5px',
        }}
      >
        <span style={{ color: theme.colors.accent }}><Icons.Customize /></span>
        <span>Make Viola yours — every change here is tied to your account.</span>
      </div>
    );
  }

  return (
    <div
      role="region"
      aria-label="Welcome to Customize"
      style={{
        position: 'relative',
        margin: '0 0 24px',
        padding: '20px 22px 18px',
        borderRadius: '16px',
        backgroundColor: theme.colors.accentHover,
        border: `1px solid ${theme.colors.accentBorder}`,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '14px' }}>
        <div
          aria-hidden="true"
          style={{
            flex: '0 0 auto',
            width: '36px', height: '36px',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            borderRadius: '10px',
            backgroundColor: theme.colors.bgElevated,
            color: theme.colors.accent,
          }}
        >
          <Icons.Customize />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{
            margin: 0,
            fontSize: '16px',
            fontWeight: 600,
            color: theme.colors.textPrimary,
            letterSpacing: '-0.01em',
          }}>
            Make Viola yours
          </h2>
          <p style={{
            margin: '6px 0 0',
            fontSize: '13.5px',
            lineHeight: 1.55,
            color: theme.colors.textSecondary,
          }}>
            Change how Viola looks, how she sounds, what she calls you, and how she hears you. The easiest changes start with a conversation — say things like <em>"call me Captain"</em> and Viola will write them down. Or change anything by hand below.
          </p>
        </div>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss welcome message"
          title="Got it"
          style={{
            flex: '0 0 auto',
            width: '44px', height: '44px',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            border: 'none',
            borderRadius: '8px',
            backgroundColor: 'transparent',
            color: theme.colors.textMuted,
            cursor: 'pointer',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = theme.colors.bgElevated; }}
          onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = 'transparent'; }}
        >
          <Icons.Close />
        </button>
      </div>
    </div>
  );
}

WelcomeBanner.propTypes = {
  theme: PropTypes.object.isRequired,
};

// ── PersonalityPreview ─────────────────────────────────────────────────────
// Reads the user's VIOLA.md (the user-editable file injected into every turn)
// and shows a friendly preview. Full editing lives in the Memory panel.

function PersonalityPreview({ theme }) {
  const [content, setContent] = useState('');
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    // VIOLA.md lives on the desktop's local disk -- a desktop-file concept
    // with no cloud-native replacement (unlike the row-based /v1/memories
    // schema MemoryPanel.jsx's cloud tab now uses -- #1182). Checked directly
    // via isCloudSurface() rather than a featureSurface.js key: `memory_files`
    // was removed 2026-07-16 once the Memory panel itself became cloud-native,
    // but this VIOLA.md preview has nothing to rewire to on the cloud SPA, so
    // it keeps skipping the dead fetch there; the preview stays empty.
    if (isCloudSurface()) {
      setContent('');
      setLoaded(true);
      return undefined;
    }
    let cancelled = false;
    (async () => {
      try {
        const resp = await authFetch('/api/memory/viola');
        const payload = resp.ok ? await resp.json() : {};
        const text = payload?.data?.content || payload?.content || '';
        if (!cancelled) setContent(text.trim());
      } catch (_e) {
        if (!cancelled) setContent('');
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const preview = useMemo(() => {
    if (!content) return '';
    const lines = content.split('\n').filter((line) => line.trim()).slice(0, 4);
    return lines.join('\n');
  }, [content]);

  const hasMore = content && content.split('\n').filter((l) => l.trim()).length > 4;

  return (
    <div style={{ padding: '16px 20px' }}>
      <div style={{
        color: theme.colors.textSecondary,
        fontSize: '13px',
        lineHeight: 1.55,
        marginBottom: '12px',
      }}>
        Viola reads <strong>VIOLA.md</strong> at the start of every turn — it's where she keeps notes about you: how to address you, things to remember, your routines, allergies, anything you want her to know.
      </div>

      <div
        style={{
          padding: '14px 16px',
          borderRadius: '12px',
          backgroundColor: theme.colors.bgCard,
          border: `1px solid ${theme.colors.borderSubtle}`,
          color: content ? theme.colors.textPrimary : theme.colors.textMuted,
          fontFamily: "'JetBrains Mono', 'Consolas', 'Menlo', monospace",
          fontSize: '12.5px',
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          minHeight: '60px',
          maxHeight: '160px',
          overflow: 'hidden',
          position: 'relative',
        }}
      >
        {!loaded ? '…' : (preview || 'Empty. Tell Viola something about yourself in conversation and she\'ll write it here, or open the Memory panel to edit directly.')}
        {hasMore && (
          <div style={{
            position: 'absolute',
            bottom: 0, left: 0, right: 0,
            height: '40px',
            background: `linear-gradient(180deg, transparent 0%, ${theme.colors.bgCard} 100%)`,
            pointerEvents: 'none',
          }} />
        )}
      </div>

      <div style={{
        marginTop: '12px',
        color: theme.colors.textMuted,
        fontSize: '12px',
        lineHeight: 1.5,
      }}>
        To edit, open the <strong>Memory</strong> panel (top right of the main window). The <em>Viola</em> tab there is the editor for this file.
      </div>
    </div>
  );
}

PersonalityPreview.propTypes = {
  theme: PropTypes.object.isRequired,
};

// ── ResetSection ────────────────────────────────────────────────────────────

function ResetSection({ onReset, theme }) {
  const [confirming, setConfirming] = useState(false);

  if (!confirming) {
    return (
      <div style={{ padding: '16px 20px' }}>
        <div style={{
          color: theme.colors.textSecondary,
          fontSize: '13px',
          lineHeight: 1.5,
          marginBottom: '14px',
        }}>
          Restore appearance and voice settings to their defaults. This will not touch your account, memory, capabilities, or AI configuration.
        </div>
        <FooterButton variant="secondary" onClick={() => setConfirming(true)}>
          Reset appearance to defaults
        </FooterButton>
      </div>
    );
  }

  return (
    <div style={{ padding: '16px 20px' }}>
      <div style={{
        color: theme.colors.textPrimary,
        fontSize: '13px',
        lineHeight: 1.5,
        marginBottom: '14px',
        padding: '12px 14px',
        backgroundColor: `${theme.colors.statusYellow}12`,
        border: `1px solid ${theme.colors.statusYellow}30`,
        borderRadius: '10px',
      }}>
        Reset accent color and theme to their defaults?
      </div>
      <div style={{ display: 'flex', gap: '10px' }}>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          style={{
            padding: '9px 16px',
            borderRadius: '10px',
            border: `1px solid ${theme.colors.borderLight}`,
            backgroundColor: 'transparent',
            color: theme.colors.textSecondary,
            cursor: 'pointer',
            fontSize: '13px',
            fontWeight: 500,
          }}
        >
          Cancel
        </button>
        <DangerButton
          color={theme.colors.statusRed}
          onClick={() => { setConfirming(false); onReset(); }}
        >
          Yes, reset
        </DangerButton>
      </div>
    </div>
  );
}

ResetSection.propTypes = {
  onReset: PropTypes.func.isRequired,
  theme: PropTypes.object.isRequired,
};

// ── WeatherLocationField ────────────────────────────────────────────────────
// The TopBar's "Set your location" chip opens this tab (SmartDisplay.jsx ->
// TopBar.jsx onOpenWeatherSettings), and SmartDisplay only fetches weather once
// `weather_location` is set — so without this input the chip was a dead end and
// weather could never turn on from a browser. The setting is cloud user-scoped
// (ui/settings_schema.py USER_SETTING_KEYS), and the cloud weather route
// resolves the signed-in caller's own saved value
// (ui/api/routes/weather.py:_resolve_cloud_user_location, #3564), so the same
// field works on desktop and on the cloud SPA.

function WeatherLocationField({ value, onChange, theme }) {
  const inputId = React.useId();

  return (
    <div style={{ padding: '16px 20px' }}>
      <label
        htmlFor={inputId}
        style={{
          display: 'block',
          color: theme.colors.textPrimary,
          fontSize: '14px',
          fontWeight: 500,
        }}
      >
        Location
      </label>
      <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '4px', lineHeight: 1.5 }}>
        The city Viola shows weather for. A city and state, a postal code, or "City, Country" all work. Leave it empty and Viola falls back to the approximate location of your internet connection.
      </div>
      <input
        id={inputId}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. Milwaukee, WI"
        autoComplete="off"
        style={{
          width: '100%',
          marginTop: '12px',
          minHeight: '44px',
          padding: '12px 16px',
          borderRadius: '12px',
          border: `1px solid ${theme.colors.borderLight}`,
          backgroundColor: theme.colors.bgElevated,
          color: theme.colors.textPrimary,
          fontSize: '14px',
          fontFamily: 'inherit',
          outline: 'none',
          boxSizing: 'border-box',
        }}
      />
    </div>
  );
}

WeatherLocationField.propTypes = {
  value: PropTypes.string.isRequired,
  onChange: PropTypes.func.isRequired,
  theme: PropTypes.object.isRequired,
};

// ── CustomizeTab (main) ─────────────────────────────────────────────────────
// All aesthetics: how Viola looks, how she calls you, how she hears you.
// Functional capability decisions (tier / autonomy / what she can touch) live
// in the AI & Agents tab — they are NOT customization, they are configuration.

const CustomizeTab = React.memo(function CustomizeTab({
  localSettings,
  updateLocal,
}) {
  const theme = THEME;

  const onReset = useCallback(() => {
    // Restore canonical defaults for the aesthetic surface only.
    updateLocal('accent_color', '');   // empty = backend default (mahogany)
    updateLocal('theme', 'dark');
    applyTheme('dark');
  }, [updateLocal]);

  return (
    <>
      <div style={{ padding: '0 20px' }}>
        <WelcomeBanner theme={theme} />
      </div>

      {/* ── Look ─────────────────────────────────────────────────────────── */}
      <Section title="Look">
        <div style={{ padding: '16px 20px' }}>
          <Select
            label="Color Theme"
            tooltip="Choose Viola's color scheme. 'Follow System' matches your OS light/dark setting."
            value={localSettings.theme || 'dark'}
            onChange={(v) => {
              // Apply the theme immediately, exactly like the accent picker
              // (AccentPicker -> setAccent). Selecting a theme used to only stage
              // the value and rely on a save -> backend -> WebSocket round-trip to
              // SmartDisplay's applyTheme effect; when that propagation didn't
              // reach the render surface the theme silently never applied
              // (issue #769). Applying here makes the control actually work and
              // persists the choice via applyTheme's localStorage cache.
              updateLocal('theme', v);
              applyTheme(v);
            }}
            options={[
              { value: 'dark', label: 'Dark (Default)' },
              { value: 'light', label: 'Light' },
              { value: 'system', label: 'Follow System' },
            ]}
          />
        </div>
        <SectionDivider />
        <div style={{ padding: '16px 20px' }}>
          <div style={{ marginBottom: '12px' }}>
            <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 500 }}>
              Accent color
            </div>
            <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '4px', lineHeight: 1.5 }}>
              The highlight color used across Viola's interface — buttons, links, and the playback bar.
            </div>
          </div>
          <AccentPicker onChange={(hex) => updateLocal('accent_color', hex)} />
        </div>
      </Section>

      {/* ── Weather ──────────────────────────────────────────────────────── */}
      <Section title="Weather">
        <WeatherLocationField
          value={localSettings.weather_location || ''}
          onChange={(v) => updateLocal('weather_location', v)}
          theme={theme}
        />
      </Section>

      {/* ── Personality ──────────────────────────────────────────────────── */}
      <Section title="Personality">
        <PersonalityPreview theme={theme} />
      </Section>

      {/* ── Voice & Wake Word ────────────────────────────────────────────── */}
      <Section title="Voice & Wake Word">
        <div style={{
          padding: '14px 20px 0',
          color: theme.colors.textSecondary,
          fontSize: '13px',
          lineHeight: 1.55,
        }}>
          Custom wake-word training is currently unavailable. The default is <strong>"Viola"</strong>; existing local models can still be switched here.
        </div>
        <WakeWordSection theme={theme} />
      </Section>

      {/* ── Reset ────────────────────────────────────────────────────────── */}
      <Section title="Reset">
        <ResetSection onReset={onReset} theme={theme} />
      </Section>
    </>
  );
});

CustomizeTab.propTypes = {
  localSettings: PropTypes.object.isRequired,
  updateLocal: PropTypes.func.isRequired,
};

export default CustomizeTab;
