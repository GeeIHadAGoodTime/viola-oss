import React, { useCallback, useEffect, useState } from 'react';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';
import { isFeatureHidden } from '../../utils/featureSurface';
import { useCloudConsentGate } from '../../hooks/cloudConsentGate';
import DesktopUpsell from '../DesktopUpsell';
import { Section, SectionDivider } from '../settings';
import MCPServerList from './MCPServerList';
import PluginList from './PluginList';
import RegisterMCPForm from './RegisterMCPForm';
import SuggestedExtensions from './SuggestedExtensions';

const theme = THEME;

function getArray(data, key) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.[key])) return data[key];
  if (Array.isArray(data?.data?.[key])) return data.data[key];
  return [];
}

const ExtensionsSection = React.memo(() => {
  // Settings is reachable before a user has ever run a turn, so these two
  // buttons are genuine first-turn entry points and take the shared gate (#362).
  const interceptCloudConsent = useCloudConsentGate();
  const [servers, setServers] = useState([]);
  const [plugins, setPlugins] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [loading, setLoading] = useState(true);
  // Status banner: { text, isError } or null. Tracking the kind explicitly
  // avoids guessing error-vs-success from the message text — the old
  // `.includes('failed')` heuristic left real errors like "Could not load
  // extensions." rendered in the neutral (non-error) colour.
  const [status, setStatus] = useState(null);
  const [registerEntry, setRegisterEntry] = useState(null);
  const [registerOpen, setRegisterOpen] = useState(false);

  const loadAll = useCallback(async () => {
    // #4226: `/v1/extensions/*` is the LOCAL_ONLY `extensions` route group
    // (backend/cloud_route_manifest.py) — it mutates local MCP-server and
    // plugin PROCESS state on the user's own machine, so cloud never serves
    // it. Without this the cloud SPA fired three dead fetches on mount and
    // then rendered live Register/Enable/Remove buttons over the failure.
    if (isFeatureHidden('extensions')) {
      setServers([]);
      setPlugins([]);
      setCatalog([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setStatus(null);
    try {
      const [mcpData, pluginData, suggestedData] = await Promise.all([
        apiFetch('/v1/extensions/mcp'),
        apiFetch('/v1/extensions/plugins'),
        apiFetch('/v1/extensions/suggested'),
      ]);
      setServers(getArray(mcpData, 'servers'));
      setPlugins(getArray(pluginData, 'plugins'));
      setCatalog(getArray(suggestedData, 'catalog'));
    } catch (err) {
      setStatus({ text: err?.message || 'Could not load extensions.', isError: true });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const postAction = useCallback(async (path, options = {}) => {
    setStatus(null);
    try {
      await apiFetch(path, { method: 'POST', ...options });
      await loadAll();
    } catch (err) {
      setStatus({ text: err?.message || 'Extension action failed.', isError: true });
    }
  }, [loadAll]);

  const removeMcp = useCallback(async (name) => {
    setStatus(null);
    try {
      await apiFetch(`/v1/extensions/mcp/${encodeURIComponent(name)}`, { method: 'DELETE' });
      await loadAll();
    } catch (err) {
      setStatus({ text: err?.message || 'Could not remove MCP server.', isError: true });
    }
  }, [loadAll]);

  const submitMcp = useCallback(async (payload) => {
    await apiFetch('/v1/extensions/mcp/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    setStatus({ text: `Registered ${payload.name}.`, isError: false });
    await loadAll();
  }, [loadAll]);

  const installSuggested = useCallback(async (entry) => {
    setStatus(null);
    if (entry.command_install_prompt) {
      if (interceptCloudConsent({ kind: 'text', text: entry.command_install_prompt })) return;
      try {
        await apiFetch('/v1/command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: entry.command_install_prompt }),
        });
        setStatus({ text: `Sent ${entry.name} install request to Viola.`, isError: false });
        setRegisterOpen(false);
        return;
      } catch {
        // Fall through to the deterministic manual registration form.
      }
    }
    setRegisterEntry(entry);
    setRegisterOpen(true);
  }, [interceptCloudConsent]);

  const openRegisterForm = useCallback(() => {
    setRegisterEntry(null);
    setRegisterOpen(true);
  }, []);

  const openPluginFolder = useCallback(async (plugin) => {
    setStatus(null);
    const text = `Open the plugin folder for ${plugin.name}.`;
    if (interceptCloudConsent({ kind: 'text', text })) return;
    try {
      await apiFetch('/v1/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      setStatus({ text: `Asked Viola to open ${plugin.name}.`, isError: false });
    } catch (err) {
      setStatus({ text: err?.message || 'Could not open plugin folder.', isError: true });
    }
  }, [interceptCloudConsent]);

  // Every control below drives the local extension host. On the cloud SPA the
  // honest surface is the upsell, not a panel of buttons that cannot work.
  if (isFeatureHidden('extensions')) {
    return (
      <Section title="Extensions">
        <div style={{ padding: '16px 20px' }}>
          <DesktopUpsell feature="extensions" />
        </div>
      </Section>
    );
  }

  return (
    <>
      <Section title="Extensions">
        <div style={{ padding: '16px 20px', display: 'grid', gap: '18px' }}>
          {loading ? (
            <div style={{ color: theme.colors.textMuted, fontSize: '13px' }}>Loading extensions...</div>
          ) : (
            <>
              <MCPServerList
                servers={servers}
                onRegister={openRegisterForm}
                onDisable={(name) => postAction(`/v1/extensions/mcp/${encodeURIComponent(name)}/disable`)}
                onReconnect={(name) => postAction(`/v1/extensions/mcp/${encodeURIComponent(name)}/enable`)}
                onRemove={removeMcp}
              />
              <SectionDivider />
              <PluginList
                plugins={plugins}
                onEnable={(name) => postAction(`/v1/extensions/plugins/${encodeURIComponent(name)}/enable`)}
                onDisable={(name) => postAction(`/v1/extensions/plugins/${encodeURIComponent(name)}/disable`)}
                onReload={() => postAction('/v1/extensions/plugins/reload')}
                onOpenFolder={openPluginFolder}
              />
              <SectionDivider />
              <SuggestedExtensions catalog={catalog} onInstall={installSuggested} />
            </>
          )}
          {status && (
            <div
              role={status.isError ? 'alert' : undefined}
              style={{ color: status.isError ? theme.colors.statusRed : theme.colors.textSecondary, fontSize: '12px' }}
            >
              {status.text}
            </div>
          )}
        </div>
      </Section>
      <RegisterMCPForm
        entry={registerEntry}
        isOpen={registerOpen}
        onClose={() => setRegisterOpen(false)}
        onSubmit={submitMcp}
      />
    </>
  );
});

export default ExtensionsSection;
