import React, { useCallback, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';
import { isFeatureHidden } from '../../utils/featureSurface';
import DesktopUpsell from '../DesktopUpsell';
import ExtensionsSection from '../extensions/ExtensionsSection';
import {
  CloseButton,
  FooterButton,
  Section,
  SectionDivider,
  Select,
  Slider,
  Toggle,
  SettingRow,
  ScrollbarStyles,
} from '../settings';

const theme = THEME;

const REASONING_LEVELS = ['none', 'low', 'medium', 'high', 'xhigh'];
const CODEX_LEVELS = ['minimal', 'low', 'medium', 'high'];

function levelIndex(value, levels) {
  const index = levels.indexOf(value);
  return index === -1 ? 0 : index;
}

function textInputStyle(width = '100%') {
  return {
    width,
    padding: '12px 16px',
    borderRadius: '12px',
    border: `1px solid ${theme.colors.borderLight}`,
    backgroundColor: theme.colors.bgCard,
    color: theme.colors.textPrimary,
    fontSize: '14px',
    outline: 'none',
    boxSizing: 'border-box',
  };
}

function smallButtonStyle({ primary = false, danger = false, disabled = false } = {}) {
  return {
    padding: '8px 14px',
    borderRadius: '8px',
    border: primary ? 'none' : `1px solid ${danger ? theme.colors.statusRed : theme.colors.borderLight}`,
    backgroundColor: primary ? theme.colors.accent : 'transparent',
    color: primary ? '#fff' : danger ? theme.colors.statusRed : theme.colors.textSecondary,
    fontSize: '13px',
    fontWeight: primary ? 600 : 500,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.55 : 1,
  };
}

function matchesSection(section, query) {
  if (!query.trim()) return true;
  return section.searchText.toLowerCase().includes(query.trim().toLowerCase());
}

const AdvancedSettingsWindow = React.memo(({
  isOpen,
  onClose,
  settings,
  onSettingChange,
  onSettingsChange,
}) => {
  const [search, setSearch] = useState('');
  const [diagnosticStatus, setDiagnosticStatus] = useState('');
  const [diagnosticBusy, setDiagnosticBusy] = useState('');
  const [resetBusy, setResetBusy] = useState(false);
  const safeSettings = settings || {};
  const aiSource = safeSettings.ai_source || 'managed';
  const showProviderOptions = ['byok', 'codex'].includes(aiSource);

  const runDiagnostic = useCallback(async (name, path, options = {}) => {
    // #4226: every probe in this window dials a route group cloud does not
    // serve (`diagnostics` LOCAL_ONLY, `smarthome` COMPANION_REQUIRED,
    // `codex_auth` LOCAL_ONLY — backend/cloud_route_manifest.py). The window
    // itself is walled below, so this is the belt to that braces: a direct
    // mount still cannot fire a probe that can only 404.
    if (isFeatureHidden('system_controls')) {
      return;
    }
    setDiagnosticBusy(name);
    setDiagnosticStatus('');
    try {
      const result = await apiFetch(path, options);
      if (name === 'Smart home') {
        const connected = Boolean(result?.connected);
        setDiagnosticStatus(`${name}: ${connected ? 'connected' : result?.message || 'not connected'}`);
      } else {
        setDiagnosticStatus(`${name}: ${result?.status || result?.message || 'ok'}`);
      }
    } catch (err) {
      setDiagnosticStatus(`${name}: ${err?.message || 'failed'}`);
    } finally {
      setDiagnosticBusy('');
    }
  }, []);

  const handleReset = useCallback(async () => {
    // `/v1/settings/reset` has no cloud RouteGroup at all, so on the cloud SPA
    // this was a confirm dialog followed by a 404 (#4226).
    if (isFeatureHidden('system_controls')) {
      return;
    }
    if (typeof window !== 'undefined' && !window.confirm('Reset all settings to defaults?')) {
      return;
    }
    setResetBusy(true);
    try {
      const result = await apiFetch('/v1/settings/reset', { method: 'POST' });
      const nextSettings = result?.settings || result?.data?.settings;
      if (nextSettings && onSettingsChange) {
        onSettingsChange(nextSettings);
      }
      setDiagnosticStatus('Settings reset to defaults.');
    } catch (err) {
      setDiagnosticStatus(`Reset failed: ${err?.message || 'unknown error'}`);
    } finally {
      setResetBusy(false);
    }
  }, [onSettingsChange]);

  const sections = useMemo(() => [
    {
      id: 'extensions',
      title: 'Extensions',
      searchText: 'extensions mcp servers plugins suggested install register reconnect disable remove reload tools',
      render: () => <ExtensionsSection />,
    },
    {
      id: 'debug',
      title: 'Diagnostics',
      searchText: 'diagnostics mode detail level activity details visible browser diagnostics',
      render: () => (
        <Section title="Diagnostics">
          <SettingRow title="Diagnostics Mode" description="Show detailed diagnostics and advanced activity details.">
            <Toggle
              checked={Boolean(safeSettings.developer_mode)}
              onChange={(v) => onSettingChange('developer_mode', v)}
              ariaLabel="Diagnostics mode"
            />
          </SettingRow>
          <SectionDivider />
          <div style={{ padding: '16px 20px' }}>
            <Select
              label="Diagnostic detail"
              value={safeSettings.log_level || 'INFO'}
              onChange={(v) => onSettingChange('log_level', v)}
              options={[
                { value: 'DEBUG', label: 'Detailed' },
                { value: 'INFO', label: 'Standard' },
                { value: 'WARNING', label: 'Warnings only' },
                { value: 'ERROR', label: 'Errors only' },
              ]}
            />
          </div>
          <SectionDivider />
          <div style={{ padding: '16px 20px' }}>
            <button
              type="button"
              onClick={() => runDiagnostic('Diagnostics', '/v1/diagnostics/logs')}
              disabled={diagnosticBusy === 'Diagnostics'}
              style={smallButtonStyle()}
            >
              {diagnosticBusy === 'Diagnostics' ? 'Opening...' : 'Open Diagnostics'}
            </button>
          </div>
        </Section>
      ),
    },
    {
      id: 'provider',
      title: 'Provider tuning',
      searchText: 'provider tuning byok codex reasoning routing agent codex effort sliders provider model base url api key',
      render: () => (
        <Section title="Provider tuning">
          <div style={{ padding: '16px 20px', display: 'grid', gap: '18px' }}>
            <Slider
              label={`Routing reasoning: ${safeSettings.routing_reasoning_effort || 'low'}`}
              min={0}
              max={REASONING_LEVELS.length - 1}
              value={levelIndex(safeSettings.routing_reasoning_effort || 'low', REASONING_LEVELS)}
              onChange={(idx) => onSettingChange('routing_reasoning_effort', REASONING_LEVELS[idx])}
            />
            <Slider
              label={`Agent reasoning: ${safeSettings.agent_reasoning_effort || 'medium'}`}
              min={0}
              max={REASONING_LEVELS.length - 1}
              value={levelIndex(safeSettings.agent_reasoning_effort || 'medium', REASONING_LEVELS)}
              onChange={(idx) => onSettingChange('agent_reasoning_effort', REASONING_LEVELS[idx])}
            />
            <Slider
              label={`Codex reasoning: ${safeSettings.codex_reasoning_effort || 'medium'}`}
              min={0}
              max={CODEX_LEVELS.length - 1}
              value={levelIndex(safeSettings.codex_reasoning_effort || 'medium', CODEX_LEVELS)}
              onChange={(idx) => onSettingChange('codex_reasoning_effort', CODEX_LEVELS[idx])}
            />
          </div>
          {showProviderOptions && (
            <>
              <SectionDivider />
              <div style={{ padding: '16px 20px', display: 'grid', gap: '14px' }}>
                <Select
                  label="Provider"
                  value={safeSettings.llm_provider || 'openai'}
                  onChange={(v) => onSettingChange('llm_provider', v)}
                  options={[
                    { value: 'openai', label: 'OpenAI' },
                    { value: 'anthropic', label: 'Anthropic' },
                    { value: 'google', label: 'Google' },
                    { value: 'ollama', label: 'Ollama' },
                    { value: 'openai_compatible', label: 'OpenAI-compatible' },
                  ]}
                />
                <label style={{ display: 'grid', gap: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                  Model
                  <input
                    type="text"
                    value={safeSettings.llm_model || ''}
                    onChange={(e) => onSettingChange('llm_model', e.target.value)}
                    placeholder="Provider default"
                    style={textInputStyle()}
                  />
                </label>
                <label style={{ display: 'grid', gap: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                  Base URL
                  <input
                    type="url"
                    value={safeSettings.llm_base_url || ''}
                    onChange={(e) => onSettingChange('llm_base_url', e.target.value)}
                    placeholder="Optional custom endpoint"
                    style={textInputStyle()}
                  />
                </label>
              </div>
            </>
          )}
        </Section>
      ),
    },
    {
      id: 'network',
      title: 'Network',
      searchText: 'network api port writable restart required local server',
      render: () => (
        <Section title="Network">
          <div style={{ padding: '16px 20px' }}>
            <label style={{ display: 'grid', gap: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
              API Port
              <input
                type="number"
                min={1}
                max={65535}
                value={safeSettings.api_port || 8756}
                onChange={(e) => onSettingChange('api_port', Number(e.target.value))}
                style={textInputStyle('160px')}
              />
            </label>
            <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '8px' }}>
              Restart required after changing the API port.
            </div>
          </div>
        </Section>
      ),
    },
    {
      id: 'diagnostics',
      title: 'Diagnostics',
      searchText: 'diagnostics integration test youtube calendar codex smart home connection',
      render: () => (
        <Section title="Diagnostics">
          <div style={{ padding: '16px 20px', display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={() => runDiagnostic('YouTube', '/v1/diagnostics/youtube', { method: 'POST' })}
              disabled={diagnosticBusy === 'YouTube'}
              style={smallButtonStyle()}
            >
              YouTube
            </button>
            <button
              type="button"
              onClick={() => runDiagnostic('Calendar', '/v1/calendar/status')}
              disabled={diagnosticBusy === 'Calendar'}
              style={smallButtonStyle()}
            >
              Calendar
            </button>
            <button
              type="button"
              onClick={() => runDiagnostic('Codex', '/v1/codex/auth/status')}
              disabled={diagnosticBusy === 'Codex'}
              style={smallButtonStyle()}
            >
              Codex
            </button>
            <button
              type="button"
              onClick={() => runDiagnostic('Smart home', '/v1/smarthome/test-connection', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  home_assistant_url: safeSettings.home_assistant_url || '',
                  home_assistant_token: safeSettings.home_assistant_token || '',
                }),
              })}
              disabled={diagnosticBusy === 'Smart home'}
              style={smallButtonStyle()}
            >
              Smart-home
            </button>
          </div>
          {diagnosticStatus && (
            <>
              <SectionDivider />
              <div style={{ padding: '12px 20px', color: theme.colors.textSecondary, fontSize: '13px' }}>
                {diagnosticStatus}
              </div>
            </>
          )}
        </Section>
      ),
    },
    {
      id: 'danger',
      title: 'Danger Zone',
      searchText: 'danger zone reset all settings defaults',
      render: () => (
        <Section title="Danger Zone">
          <div style={{ padding: '16px 20px', display: 'flex', justifyContent: 'space-between', gap: '16px', alignItems: 'center' }}>
            <div>
              <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 500 }}>Reset All Settings</div>
              <div style={{ color: theme.colors.textMuted, fontSize: '13px', marginTop: '2px' }}>Restore defaults for this desktop profile.</div>
            </div>
            <button
              type="button"
              onClick={handleReset}
              disabled={resetBusy}
              style={smallButtonStyle({ danger: true, disabled: resetBusy })}
            >
              {resetBusy ? 'Resetting...' : 'Reset'}
            </button>
          </div>
        </Section>
      ),
    },
  ], [
    aiSource,
    diagnosticBusy,
    diagnosticStatus,
    handleReset,
    onSettingChange,
    resetBusy,
    runDiagnostic,
    safeSettings,
    showProviderOptions,
  ]);

  // #4226: this whole window IS the "Advanced Settings window" that
  // featureSurface.js scopes to `system_controls` — tray/boot/port/audio
  // enumeration, the local updater, the local-log diagnostics and the settings
  // reset. A browser tab has none of those, and every section here dials a
  // route group cloud deliberately does not serve. Show one honest card
  // instead of a search box over controls that cannot work.
  const systemControlsHidden = isFeatureHidden('system_controls');
  const visibleSections = systemControlsHidden
    ? [{
      id: 'desktop-only',
      title: 'Desktop settings',
      searchText: '',
      render: () => (
        <Section title="Desktop settings">
          <div style={{ padding: '16px 20px' }}>
            <DesktopUpsell feature="system_controls" />
          </div>
        </Section>
      ),
    }]
    : sections.filter((section) => matchesSection(section, search));

  if (!isOpen) {
    return null;
  }

  return (
    <>
      <ScrollbarStyles />
      <div
        role="presentation"
        onClick={onClose}
        style={{
          position: 'fixed',
          inset: 0,
          zIndex: 1100,
          backgroundColor: theme.colors.overlay,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '24px',
          boxSizing: 'border-box',
          fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",
        }}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="advanced-settings-title"
          onClick={(e) => e.stopPropagation()}
          style={{
            width: 'min(760px, 100%)',
            maxHeight: 'min(82vh, calc(100dvh - 48px))',
            backgroundColor: theme.colors.bgCard,
            borderRadius: '20px',
            boxShadow: `0 24px 80px ${theme.colors.shadowDeep}, inset 0 0 0 1px ${theme.colors.borderLight}`,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '18px 22px',
            borderBottom: `1px solid ${theme.colors.borderLight}`,
            flexShrink: 0,
          }}>
            <h2 id="advanced-settings-title" style={{ margin: 0, color: theme.colors.textPrimary, fontSize: '18px', fontWeight: 600 }}>
              Advanced Settings
            </h2>
            <CloseButton onClick={onClose} />
          </div>

          <div style={{ padding: '14px 18px', borderBottom: `1px solid ${theme.colors.borderSubtle}`, flexShrink: 0 }}>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search advanced settings"
              aria-label="Search advanced settings"
              style={textInputStyle()}
            />
          </div>

          <div
            className="viola-scrollbar"
            style={{
              flex: 1,
              overflowY: 'auto',
              padding: '20px',
            }}
          >
            {visibleSections.length > 0 ? (
              visibleSections.map((section) => (
                <React.Fragment key={section.id}>
                  {section.render()}
                </React.Fragment>
              ))
            ) : (
              <div style={{ color: theme.colors.textMuted, fontSize: '13px', padding: '24px', textAlign: 'center' }}>
                No advanced settings match "{search}".
              </div>
            )}
          </div>

          <div style={{
            display: 'flex',
            justifyContent: 'flex-end',
            padding: '14px 20px',
            borderTop: `1px solid ${theme.colors.borderLight}`,
            flexShrink: 0,
          }}>
            <FooterButton variant="secondary" onClick={onClose}>Close</FooterButton>
          </div>
        </div>
      </div>
    </>
  );
});

AdvancedSettingsWindow.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  settings: PropTypes.object,
  onSettingChange: PropTypes.func.isRequired,
  onSettingsChange: PropTypes.func,
};

export default AdvancedSettingsWindow;
