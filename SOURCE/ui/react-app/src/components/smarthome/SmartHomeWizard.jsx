import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';
import { isFeatureHidden } from '../../utils/featureSurface';
import DesktopUpsell from '../DesktopUpsell';
import { Section, SettingRow, Toggle } from '../settings';
import ConnectedHubsList, { getConfiguredHubs } from './ConnectedHubsList';
import DiscoveredDeviceCard from './DiscoveredDeviceCard';
import HASetupForm from './HASetupForm';

const theme = THEME;
const noop = () => {};

function normalizeSettings(settings) {
  return settings && typeof settings === 'object' ? settings : {};
}

function buttonStyle(disabled) {
  return {
    padding: '8px 14px',
    borderRadius: '8px',
    border: 'none',
    backgroundColor: disabled ? theme.colors.bgElevated : theme.colors.accent,
    color: disabled ? theme.colors.textMuted : '#fff',
    fontSize: '13px',
    fontWeight: 600,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.7 : 1,
  };
}

function getDevices(data) {
  return Array.isArray(data?.devices) ? data.devices : [];
}

const SmartHomeWizard = React.memo(({
  settings = null,
  onSettingChange = noop,
  onSettingsChange = noop,
  autoLoadSettings = true,
}) => {
  const controlledSettings = useMemo(() => normalizeSettings(settings), [settings]);
  const [localSettings, setLocalSettings] = useState(controlledSettings);
  const [loadingSettings, setLoadingSettings] = useState(autoLoadSettings && !settings);
  const [devices, setDevices] = useState([]);
  const [hasScanned, setHasScanned] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [savingDiscovery, setSavingDiscovery] = useState(false);
  const [error, setError] = useState('');
  const [activeSetupDevice, setActiveSetupDevice] = useState(null);

  // #4226: `/v1/smarthome/*` is the COMPANION_REQUIRED `smarthome` route group
  // (backend/cloud_route_manifest.py) — discovery scans the user's own LAN and
  // Home Assistant setup needs a local companion, so cloud serves none of it.
  // Ungated, this wizard mounted on the cloud Connections tab and offered a
  // "Scan" button that could only ever fail.
  const smartHomeHidden = isFeatureHidden('smarthome');

  useEffect(() => {
    if (settings) {
      setLocalSettings(controlledSettings);
    }
  }, [controlledSettings, settings]);

  useEffect(() => {
    if (smartHomeHidden || !autoLoadSettings || settings) {
      return undefined;
    }

    let cancelled = false;

    async function loadSettings() {
      setLoadingSettings(true);
      try {
        const data = await apiFetch('/v1/settings');
        if (!cancelled) {
          setLocalSettings(normalizeSettings(data?.settings));
        }
      } catch {
        if (!cancelled) {
          setError('Could not load smart-home settings.');
        }
      } finally {
        if (!cancelled) {
          setLoadingSettings(false);
        }
      }
    }

    loadSettings();

    return () => {
      cancelled = true;
    };
  }, [smartHomeHidden, autoLoadSettings, settings]);

  const [detectedHub, setDetectedHub] = useState(null);
  const discoveryEnabled = !!localSettings.network_discovery_enabled;
  const configuredHubs = useMemo(
    () => getConfiguredHubs(localSettings, detectedHub),
    [localSettings, detectedHub],
  );
  const hasConfiguredHub = configuredHubs.length > 0;

  // Probe the configured hub once so the UI can show its REAL identity
  // (Home Assistant + the home's name/version + a verified Connected status)
  // instead of a generic placeholder. Best-effort: failures leave it
  // "Configured".
  const hubUrl = localSettings.home_assistant_url || '';
  const hubToken = localSettings.home_assistant_token || '';
  useEffect(() => {
    if (smartHomeHidden || loadingSettings || !hubUrl || !hubToken) {
      setDetectedHub(null);
      return undefined;
    }
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch('/v1/smarthome/test-connection', {
          method: 'POST',
          body: JSON.stringify({}),
        });
        if (!cancelled && data?.connected) {
          setDetectedHub({
            connected: true,
            hub_name: data.hub_name || '',
            hub_version: data.hub_version || '',
          });
        }
      } catch {
        /* identity enrichment is best-effort */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [smartHomeHidden, hubUrl, hubToken, loadingSettings]);

  const mergeSettings = useCallback((patch, responseSettings) => {
    setLocalSettings((current) => {
      const next = {
        ...current,
        ...patch,
        ...normalizeSettings(responseSettings),
      };
      onSettingsChange(next);
      return next;
    });

    Object.entries(patch).forEach(([key, value]) => {
      onSettingChange(key, value);
    });
  }, [onSettingChange, onSettingsChange]);

  const patchSettings = useCallback(async (patch) => {
    const data = await apiFetch('/v1/settings', {
      method: 'PATCH',
      body: JSON.stringify({ settings: patch }),
    });
    mergeSettings(patch, data?.settings);
    return data;
  }, [mergeSettings]);

  const handleDiscoveryToggle = useCallback(async (enabled) => {
    const previous = discoveryEnabled;
    const patch = { network_discovery_enabled: enabled };
    setSavingDiscovery(true);
    setError('');
    setLocalSettings((current) => ({ ...current, ...patch }));

    try {
      await patchSettings(patch);
      if (!enabled) {
        setDevices([]);
        setHasScanned(false);
        setActiveSetupDevice(null);
      }
    } catch {
      setLocalSettings((current) => ({ ...current, network_discovery_enabled: previous }));
      setError('Could not update network discovery.');
    } finally {
      setSavingDiscovery(false);
    }
  }, [discoveryEnabled, patchSettings]);

  const handleDiscover = useCallback(async () => {
    if (!discoveryEnabled || scanning) {
      return;
    }

    setScanning(true);
    setError('');
    setActiveSetupDevice(null);

    try {
      const data = await apiFetch('/v1/smarthome/discover', { method: 'POST' });
      const nextDevices = getDevices(data);
      setDevices(nextDevices);
      setHasScanned(true);

      if (data?.enabled === false) {
        setLocalSettings((current) => ({ ...current, network_discovery_enabled: false }));
        setError('Network discovery is off.');
      }
    } catch (err) {
      setError(err?.message || 'Could not scan for smart-home devices.');
    } finally {
      setScanning(false);
    }
  }, [discoveryEnabled, scanning]);

  const handleSetupSaved = useCallback(({ settings: savedSettings, response }) => {
    mergeSettings(savedSettings, response?.settings);
    setActiveSetupDevice(null);
  }, [mergeSettings]);

  const renderDiscoveryState = () => {
    if (!discoveryEnabled) {
      return (
        <p style={{
          color: theme.colors.textMuted,
          fontSize: '12px',
          lineHeight: 1.4,
          margin: '10px 0 0',
        }}>
          Turn on local discovery to scan for smart-home hubs, Matter-compatible devices,
          local network bridges, media speakers, and MQTT brokers.
        </p>
      );
    }

    if (devices.length > 0) {
      return (
        <ul style={{
          display: 'grid',
          gap: '10px',
          margin: '14px 0 0',
          padding: 0,
        }}>
          {devices.map((device, index) => (
            <DiscoveredDeviceCard
              key={`${device.service_type || 'device'}-${device.ip || 'unknown'}-${device.port || 'none'}-${index}`}
              device={device}
              isConnected={hasConfiguredHub}
              onSetup={setActiveSetupDevice}
            />
          ))}
        </ul>
      );
    }

    return (
      <p style={{
        color: theme.colors.textMuted,
        fontSize: '12px',
        lineHeight: 1.4,
        margin: '10px 0 0',
      }}>
        {hasScanned
          ? 'No smart-home devices found on this network.'
          : 'Run a local scan when you are ready to find devices on this network.'}
      </p>
    );
  };

  if (smartHomeHidden) {
    return (
      <Section title="Smart Home">
        <div style={{ padding: '16px 20px' }}>
          <DesktopUpsell feature="smarthome" />
        </div>
      </Section>
    );
  }

  return (
    <Section title="Smart Home">
      <ConnectedHubsList settings={localSettings} hubs={configuredHubs} />

      <div style={{ padding: '4px 0 0' }}>
        <SettingRow
          title="Find devices on my network"
          description="Opt-in discovery runs only when you ask."
        >
          <Toggle
            checked={discoveryEnabled}
            onChange={handleDiscoveryToggle}
            disabled={loadingSettings || savingDiscovery || scanning}
            ariaLabel="Enable smart-home network discovery"
          />
        </SettingRow>
      </div>

      <div style={{ padding: '0 20px 18px 20px' }}>
        <button
          type="button"
          onClick={handleDiscover}
          disabled={!discoveryEnabled || scanning || savingDiscovery}
          aria-label="Find devices on my network"
          style={buttonStyle(!discoveryEnabled || scanning || savingDiscovery)}
        >
          {scanning ? 'Finding...' : 'Find devices on my network'}
        </button>

        {error && (
          <p style={{
            color: theme.colors.statusRed,
            fontSize: '12px',
            margin: '10px 0 0',
          }} role="alert">
            {error}
          </p>
        )}

        {renderDiscoveryState()}
      </div>

      {activeSetupDevice && (
        <HASetupForm
          device={activeSetupDevice}
          initialUrl={localSettings.home_assistant_url || ''}
          initialToken={localSettings.home_assistant_token || ''}
          onSave={handleSetupSaved}
          onCancel={() => setActiveSetupDevice(null)}
        />
      )}
    </Section>
  );
});

SmartHomeWizard.propTypes = {
  settings: PropTypes.object,
  onSettingChange: PropTypes.func,
  onSettingsChange: PropTypes.func,
  autoLoadSettings: PropTypes.bool,
};

export default SmartHomeWizard;
