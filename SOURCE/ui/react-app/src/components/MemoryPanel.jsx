import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { authFetch } from '../hooks/useViolaApi';
import Markdown from './stage/modes/chat/markdown';
import ErrorBoundary from './ErrorBoundary';
import DesktopUpsell from './DesktopUpsell';
import { isFeatureHidden } from '../utils/featureSurface';
import { isCloudSurface } from './auth/cloudSurface';

const BRONZE = 'var(--accent)';
const BRONZE_FILL_LOW = 'color-mix(in srgb, var(--accent) 9%, transparent)';
const BRONZE_FILL_MED = 'color-mix(in srgb, var(--accent) 13%, transparent)';
const PANEL_FONT = "'Segoe UI', 'SF Pro Display', -apple-system, BlinkMacSystemFont, sans-serif";
// The desktop app edits the local VIOLA.md + D-layout topic files (ui/api/routes/memories.py);
// the cloud SPA has no such filesystem, so it never shows the 'viola' tab -- see
// isCloudSurface() below (#1182).
const DESKTOP_TABS = [
  { id: 'viola', label: 'Viola' },
  { id: 'memory', label: 'Memory' },
  { id: 'workbench', label: 'Workbench' },
];
const CLOUD_TABS = [
  { id: 'memory', label: 'Memory' },
  { id: 'workbench', label: 'Workbench' },
];
const TEMPLATES = [
  { id: 'allergies', label: '+ Allergy', text: '\n## Allergies\n- I am allergic to ...\n' },
  { id: 'doctor', label: '+ Doctor', text: '\n## Doctors\n- Doctor: ...\n' },
  { id: 'family', label: '+ Family member', text: '\n## Family\n- Name: ...\n' },
  { id: 'routine', label: '+ Routine', text: '\n## Routines\n- Routine: ...\n' },
  { id: 'always', label: '+ Always do', text: '\n## Always do\n- Always ...\n' },
  { id: 'never', label: '+ Never', text: '\n## Never\n- Never ...\n' },
];
// Matches services/memory/store.py's _VALID_CATEGORIES so a memory created from
// the cloud Memory tab looks like one Viola's own agent would file (the cloud
// /v1/memories API itself accepts any string up to 64 chars -- this is just a
// friendly, consistent picker, not a server-enforced enum).
const CLOUD_MEMORY_CATEGORIES = ['fact', 'preference', 'correction', 'routine', 'note', 'context'];

async function parseResponse(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok || payload?.ok === false) {
    const message = payload?.error?.message || payload?.message || `Request failed (${response.status})`;
    throw new Error(message);
  }
  return payload?.data !== undefined ? payload.data : payload;
}

// Cloud /v1/memories/* responses use the same {ok,error,data} envelope, but the
// Memory tab needs to distinguish "consent not granted yet" (403 consent_required
// -- show an inline prompt to enable Cloud Sync) from a real failure (show the
// error banner), so it reads the structured error code instead of throwing on
// every non-2xx like parseResponse above.
async function parseMemoryResponse(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (response.status === 403 && payload?.error?.code === 'consent_required') {
    return { consentRequired: true, data: undefined };
  }
  if (!response.ok || payload?.ok === false) {
    const message = payload?.error?.message || payload?.message || `Request failed (${response.status})`;
    throw new Error(message);
  }
  return { consentRequired: false, data: payload?.data !== undefined ? payload.data : payload };
}

function encodePath(value) {
  return encodeURIComponent(value);
}

function formatBytes(size) {
  const bytes = Number(size || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

const buttonBase = {
  borderRadius: 6,
  border: `1px solid ${THEME.colors.borderLight}`,
  backgroundColor: 'transparent',
  color: THEME.colors.textPrimary,
  cursor: 'pointer',
  fontFamily: PANEL_FONT,
  fontSize: 13,
  minHeight: 44,
};

const iconButton = {
  ...buttonBase,
  minWidth: 44,
  width: 32,
  minHeight: 44,
  height: 30,
  padding: 0,
};

function EmptyState({ children }) {
  return (
    <div
      style={{
        border: `1px dashed ${THEME.colors.borderLight}`,
        borderRadius: 8,
        padding: '18px 16px',
        color: THEME.colors.textMuted,
        backgroundColor: THEME.colors.glassBase,
        lineHeight: 1.45,
      }}
    >
      {children}
    </div>
  );
}

EmptyState.propTypes = {
  children: PropTypes.node.isRequired,
};

function MarkdownFrame({ content, muted = false }) {
  return (
    <div
      style={{
        fontFamily: PANEL_FONT,
        color: muted ? THEME.colors.textMuted : THEME.colors.textPrimary,
        fontSize: 14,
        lineHeight: 1.55,
      }}
    >
      {content.trim() ? <Markdown content={content} /> : <EmptyState>No content yet.</EmptyState>}
    </div>
  );
}

MarkdownFrame.propTypes = {
  content: PropTypes.string.isRequired,
  muted: PropTypes.bool,
};

function EntryRow({ entry, onDelete, busy }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) auto',
        gap: 10,
        alignItems: 'start',
        padding: '8px 10px',
        borderRadius: 6,
        backgroundColor: THEME.colors.glassBase,
        border: `1px solid ${THEME.colors.borderLight}`,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ color: entry.kind === 'section' ? BRONZE : THEME.colors.textPrimary, fontWeight: entry.kind === 'section' ? 700 : 500, overflowWrap: 'anywhere' }}>
          {entry.kind === 'section' ? entry.text : entry.markdown}
        </div>
        <div style={{ marginTop: 3, color: THEME.colors.textMuted, fontSize: 11 }}>
          line {entry.line_number}
        </div>
      </div>
      <button
        type="button"
        aria-label={`Delete ${entry.text}`}
        disabled={busy}
        onClick={() => onDelete(entry)}
        style={{ ...iconButton, color: THEME.colors.textMuted }}
        title="Delete"
      >
        Del
      </button>
    </div>
  );
}

EntryRow.propTypes = {
  entry: PropTypes.shape({
    id: PropTypes.string.isRequired,
    kind: PropTypes.string.isRequired,
    line_number: PropTypes.number.isRequired,
    markdown: PropTypes.string.isRequired,
    text: PropTypes.string.isRequired,
  }).isRequired,
  onDelete: PropTypes.func.isRequired,
  busy: PropTypes.bool.isRequired,
};

function EntryList({ entries, onDelete, busy }) {
  if (!entries.length) {
    return <EmptyState>No saved entries yet.</EmptyState>;
  }
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      {entries.map((entry) => (
        <EntryRow key={`${entry.id}-${entry.line_number}`} entry={entry} onDelete={onDelete} busy={busy} />
      ))}
    </div>
  );
}

EntryList.propTypes = {
  entries: PropTypes.arrayOf(EntryRow.propTypes.entry).isRequired,
  onDelete: PropTypes.func.isRequired,
  busy: PropTypes.bool.isRequired,
};

function ViolaTab({
  content,
  draft,
  editing,
  busy,
  onDraftChange,
  onEdit,
  onCancel,
  onSave,
  onInsertTemplate,
}) {
  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        {editing ? (
          <>
            <button type="button" onClick={onCancel} disabled={busy} style={{ ...buttonBase, padding: '7px 12px' }}>
              Cancel
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={busy}
              style={{ ...buttonBase, padding: '7px 14px', backgroundColor: BRONZE, borderColor: BRONZE, color: '#fff', fontWeight: 700 }}
            >
              Save
            </button>
          </>
        ) : (
          <button type="button" onClick={onEdit} disabled={busy} style={{ ...buttonBase, padding: '7px 14px', borderColor: BRONZE, color: THEME.colors.textBright }}>
            Edit
          </button>
        )}
      </div>

      {editing ? (
        <textarea
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          spellCheck="true"
          aria-label="Edit VIOLA.md"
          data-testid="viola-md-editor"
          style={{
            minHeight: 320,
            resize: 'vertical',
            boxSizing: 'border-box',
            width: '100%',
            padding: 12,
            borderRadius: 6,
            border: `1px solid ${THEME.colors.borderLight}`,
            backgroundColor: THEME.colors.glassBase,
            color: THEME.colors.textPrimary,
            fontFamily: 'Consolas, "SFMono-Regular", monospace',
            fontSize: 13,
            lineHeight: 1.5,
            outline: 'none',
          }}
        />
      ) : (
        <div
          data-testid="viola-md-rendered"
          style={{
            minHeight: 320,
            borderRadius: 8,
            border: `1px solid ${THEME.colors.borderLight}`,
            backgroundColor: THEME.colors.glassBase,
            padding: '14px 16px',
            overflow: 'auto',
          }}
        >
          <MarkdownFrame content={content} />
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {TEMPLATES.map((template) => (
          <button
            key={template.id}
            type="button"
            onClick={() => onInsertTemplate(template.text)}
            style={{ ...buttonBase, padding: '7px 10px', color: THEME.colors.textSecondary }}
          >
            {template.label}
          </button>
        ))}
      </div>
    </div>
  );
}

ViolaTab.propTypes = {
  content: PropTypes.string.isRequired,
  draft: PropTypes.string.isRequired,
  editing: PropTypes.bool.isRequired,
  busy: PropTypes.bool.isRequired,
  onDraftChange: PropTypes.func.isRequired,
  onEdit: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
  onSave: PropTypes.func.isRequired,
  onInsertTemplate: PropTypes.func.isRequired,
};

function TopicSection({ topic, expanded, busy, onToggle, onDeleteFile, onDeleteEntry }) {
  const title = topic.name.replace(/[-_]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
  return (
    <div style={{ border: `1px solid ${THEME.colors.borderLight}`, borderRadius: 8, overflow: 'hidden' }}>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) auto auto',
          gap: 8,
          alignItems: 'center',
          padding: '10px 12px',
          backgroundColor: THEME.colors.glassBase,
        }}
      >
        <button
          type="button"
          onClick={() => onToggle(topic.name)}
          aria-expanded={expanded}
          style={{ border: 0, background: 'transparent', color: THEME.colors.textBright, cursor: 'pointer', textAlign: 'left', fontFamily: PANEL_FONT, fontWeight: 700, fontSize: 14, minWidth: 0, minHeight: '44px', display: 'flex', alignItems: 'center' }}
        >
          {expanded ? 'v' : '>'} {title}
        </button>
        <button type="button" onClick={() => onToggle(topic.name)} style={{ ...buttonBase, padding: '6px 10px' }}>
          View
        </button>
        <button type="button" disabled={busy} onClick={() => onDeleteFile(topic)} style={{ ...buttonBase, padding: '6px 10px', color: THEME.colors.statusRed }}>
          Delete file
        </button>
      </div>
      {expanded && (
        <div style={{ display: 'grid', gap: 12, padding: 12 }}>
          <MarkdownFrame content={topic.content || ''} />
          <EntryList entries={topic.entries || []} busy={busy} onDelete={(entry) => onDeleteEntry(topic, entry)} />
        </div>
      )}
    </div>
  );
}

TopicSection.propTypes = {
  topic: PropTypes.shape({
    name: PropTypes.string.isRequired,
    filename: PropTypes.string.isRequired,
    content: PropTypes.string.isRequired,
    entries: PropTypes.arrayOf(EntryRow.propTypes.entry).isRequired,
  }).isRequired,
  expanded: PropTypes.bool.isRequired,
  busy: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
  onDeleteFile: PropTypes.func.isRequired,
  onDeleteEntry: PropTypes.func.isRequired,
};

function MemoryTab({ entries, busy, expandedTopics, onDeleteIndexEntry, onToggleTopic, onDeleteTopicFile, onDeleteTopicEntry }) {
  const indexEntries = entries?.index?.entries || [];
  const topics = entries?.topics || [];
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <section style={{ display: 'grid', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 }}>
          <h3 style={{ margin: 0, fontSize: 15, color: THEME.colors.textBright }}>MEMORY.md</h3>
          <span style={{ color: THEME.colors.textMuted, fontSize: 12 }}>{indexEntries.length} entries</span>
        </div>
        <EntryList entries={indexEntries} busy={busy} onDelete={onDeleteIndexEntry} />
      </section>

      <section style={{ display: 'grid', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 }}>
          <h3 style={{ margin: 0, fontSize: 15, color: THEME.colors.textBright }}>Topics</h3>
          <span style={{ color: THEME.colors.textMuted, fontSize: 12 }}>{topics.length} files</span>
        </div>
        {topics.length ? (
          <div style={{ display: 'grid', gap: 10 }}>
            {topics.map((topic) => (
              <TopicSection
                key={topic.name}
                topic={topic}
                expanded={Boolean(expandedTopics[topic.name])}
                busy={busy}
                onToggle={onToggleTopic}
                onDeleteFile={onDeleteTopicFile}
                onDeleteEntry={onDeleteTopicEntry}
              />
            ))}
          </div>
        ) : (
          <EmptyState>No topic files yet.</EmptyState>
        )}
      </section>
    </div>
  );
}

MemoryTab.propTypes = {
  entries: PropTypes.shape({
    index: PropTypes.shape({
      entries: PropTypes.arrayOf(EntryRow.propTypes.entry).isRequired,
    }),
    topics: PropTypes.arrayOf(TopicSection.propTypes.topic),
  }).isRequired,
  busy: PropTypes.bool.isRequired,
  expandedTopics: PropTypes.objectOf(PropTypes.bool).isRequired,
  onDeleteIndexEntry: PropTypes.func.isRequired,
  onToggleTopic: PropTypes.func.isRequired,
  onDeleteTopicFile: PropTypes.func.isRequired,
  onDeleteTopicEntry: PropTypes.func.isRequired,
};

function WorkbenchTab({ files, busy, dropActive, inputRef, onUpload, onDelete, onOpenFolder, onDropActive }) {
  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div
        onDragEnter={(event) => { event.preventDefault(); onDropActive(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => onDropActive(false)}
        onDrop={(event) => { event.preventDefault(); onDropActive(false); onUpload(event.dataTransfer.files); }}
        style={{
          minHeight: 118,
          borderRadius: 8,
          border: `1px dashed ${dropActive ? BRONZE : THEME.colors.borderHover}`,
          backgroundColor: dropActive ? BRONZE_FILL_LOW : THEME.colors.glassBase,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          color: THEME.colors.textSecondary,
          padding: 16,
        }}
      >
        <input ref={inputRef} type="file" multiple style={{ display: 'none' }} onChange={(event) => onUpload(event.target.files)} />
        <button type="button" onClick={() => inputRef.current?.click()} style={{ ...buttonBase, padding: '8px 14px', borderColor: BRONZE, color: THEME.colors.textBright }}>
          Upload files
        </button>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button type="button" onClick={onOpenFolder} style={{ ...buttonBase, padding: '7px 12px' }}>Open folder</button>
      </div>

      {files.length ? (
        <div style={{ display: 'grid', gap: 8 }}>
          {files.map((file) => (
            <div key={file.name} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto auto', gap: 10, alignItems: 'center', border: `1px solid ${THEME.colors.borderLight}`, borderRadius: 8, padding: '10px 12px', backgroundColor: THEME.colors.glassBase }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ color: THEME.colors.textBright, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</div>
                <div style={{ color: THEME.colors.textMuted, fontSize: 12 }}>{file.mime} | {formatDate(file.modified_at)}</div>
              </div>
              <div style={{ color: THEME.colors.textMuted, fontSize: 12 }}>{formatBytes(file.size)}</div>
              <button type="button" disabled={busy} onClick={() => onDelete(file.name)} style={{ ...buttonBase, padding: '5px 9px', color: THEME.colors.statusRed }}>Delete</button>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState>No files in Workbench yet.</EmptyState>
      )}
    </div>
  );
}

WorkbenchTab.propTypes = {
  files: PropTypes.arrayOf(PropTypes.shape({
    name: PropTypes.string.isRequired,
    size: PropTypes.number.isRequired,
    modified_at: PropTypes.string.isRequired,
    mime: PropTypes.string.isRequired,
  })).isRequired,
  busy: PropTypes.bool.isRequired,
  dropActive: PropTypes.bool.isRequired,
  inputRef: PropTypes.shape({ current: PropTypes.any }).isRequired,
  onUpload: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
  onOpenFolder: PropTypes.func.isRequired,
  onDropActive: PropTypes.func.isRequired,
};

// ── Cloud-native Memory tab (#1182) ─────────────────────────────────────────
// Rewires the browser SPA's Memory tab from the dead desktop D-layout file
// routes (/api/memory/viola, /api/memory/entries -- desktop filesystem only,
// see ui/api/routes/memories.py) onto the cloud-native /v1/memories/* API
// (ui/api/routes/cloud_memories.py -- Postgres-backed, per-user RLS,
// encrypted content, consent-gated). Structurally different schema from the
// desktop file view (rows with memory_id/category/importance_score + a
// quarantine workflow, not files/topics), so this is its own component with
// its own data fetching rather than a reskin of MemoryTab/ViolaTab above.

function CloudMemoryRow({ memory, busy, onSave, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [draftContent, setDraftContent] = useState(memory.content || '');
  const [draftCategory, setDraftCategory] = useState(memory.category || 'fact');
  const [draftCritical, setDraftCritical] = useState(Boolean(memory.critical));

  const startEdit = () => {
    setDraftContent(memory.content || '');
    setDraftCategory(memory.category || 'fact');
    setDraftCritical(Boolean(memory.critical));
    setEditing(true);
  };

  const save = async () => {
    const content = draftContent.trim();
    if (!content) return;
    await onSave(memory.id, { content, category: draftCategory, critical: draftCritical });
    setEditing(false);
  };

  return (
    <div
      style={{
        display: 'grid',
        gap: 8,
        padding: '10px 12px',
        borderRadius: 8,
        backgroundColor: THEME.colors.glassBase,
        border: `1px solid ${THEME.colors.borderLight}`,
      }}
    >
      {editing ? (
        <>
          <textarea
            value={draftContent}
            onChange={(event) => setDraftContent(event.target.value)}
            aria-label="Edit memory"
            style={{
              minHeight: 64,
              resize: 'vertical',
              boxSizing: 'border-box',
              width: '100%',
              padding: 8,
              borderRadius: 6,
              border: `1px solid ${THEME.colors.borderLight}`,
              backgroundColor: THEME.colors.bgSurface,
              color: THEME.colors.textPrimary,
              fontFamily: PANEL_FONT,
              fontSize: 13,
            }}
          />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <select
              value={draftCategory}
              onChange={(event) => setDraftCategory(event.target.value)}
              aria-label="Memory category"
              style={{ ...buttonBase, padding: '5px 8px', minHeight: 30 }}
            >
              {CLOUD_MEMORY_CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>{cat}</option>
              ))}
            </select>
            <label style={{ display: 'flex', alignItems: 'center', gap: 5, color: THEME.colors.textSecondary, fontSize: 12, minHeight: 44 }}>
              <input type="checkbox" checked={draftCritical} onChange={(event) => setDraftCritical(event.target.checked)} />
              Critical
            </label>
            <div style={{ flex: 1 }} />
            <button type="button" onClick={() => setEditing(false)} disabled={busy} style={{ ...buttonBase, padding: '6px 10px' }}>Cancel</button>
            <button
              type="button"
              onClick={save}
              disabled={busy || !draftContent.trim()}
              style={{ ...buttonBase, padding: '6px 12px', backgroundColor: BRONZE, borderColor: BRONZE, color: '#fff', fontWeight: 700 }}
            >
              Save
            </button>
          </div>
        </>
      ) : (
        <>
          <div style={{ color: THEME.colors.textPrimary, overflowWrap: 'anywhere' }}>{memory.content}</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11, color: BRONZE, border: `1px solid ${THEME.colors.borderLight}`, borderRadius: 4, padding: '1px 6px' }}>
              {memory.category}
            </span>
            {memory.critical && (
              <span style={{ fontSize: 11, color: THEME.colors.statusRed, border: `1px solid ${THEME.colors.statusRed}`, borderRadius: 4, padding: '1px 6px' }}>
                critical
              </span>
            )}
            {memory.verified && (
              <span style={{ fontSize: 11, color: THEME.colors.statusGreen, border: `1px solid ${THEME.colors.statusGreen}`, borderRadius: 4, padding: '1px 6px' }}>
                verified
              </span>
            )}
            <span style={{ fontSize: 11, color: THEME.colors.textMuted }}>{formatDate(memory.updated_at || memory.created_at)}</span>
            <div style={{ flex: 1 }} />
            <button type="button" disabled={busy} onClick={startEdit} style={{ ...buttonBase, padding: '5px 10px' }}>Edit</button>
            <button type="button" disabled={busy} onClick={() => onDelete(memory.id)} style={{ ...buttonBase, padding: '5px 10px', color: THEME.colors.statusRed }}>Delete</button>
          </div>
        </>
      )}
    </div>
  );
}

CloudMemoryRow.propTypes = {
  memory: PropTypes.shape({
    id: PropTypes.number.isRequired,
    content: PropTypes.string.isRequired,
    category: PropTypes.string.isRequired,
    critical: PropTypes.bool,
    verified: PropTypes.bool,
    created_at: PropTypes.string,
    updated_at: PropTypes.string,
  }).isRequired,
  busy: PropTypes.bool.isRequired,
  onSave: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
};

function CloudQuarantineRow({ item, busy, onRestore, onDelete }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) auto auto',
        gap: 10,
        alignItems: 'center',
        padding: '9px 12px',
        borderRadius: 8,
        backgroundColor: THEME.colors.glassBase,
        border: `1px solid ${THEME.colors.borderLight}`,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ color: THEME.colors.textPrimary, overflowWrap: 'anywhere' }}>{item.content}</div>
        <div style={{ marginTop: 3, color: THEME.colors.textMuted, fontSize: 11 }}>{item.reason}</div>
      </div>
      <button type="button" disabled={busy} onClick={() => onRestore(item.id)} style={{ ...buttonBase, padding: '6px 10px', borderColor: BRONZE, color: THEME.colors.textBright }}>
        Restore
      </button>
      <button type="button" disabled={busy} onClick={() => onDelete(item.id)} style={{ ...buttonBase, padding: '6px 10px', color: THEME.colors.statusRed }}>
        Discard
      </button>
    </div>
  );
}

CloudQuarantineRow.propTypes = {
  item: PropTypes.shape({
    id: PropTypes.number.isRequired,
    content: PropTypes.string.isRequired,
    reason: PropTypes.string,
  }).isRequired,
  busy: PropTypes.bool.isRequired,
  onRestore: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
};

function CloudMemoryTab() {
  const [loading, setLoading] = useState(true);
  const [consentRequired, setConsentRequired] = useState(false);
  const [memories, setMemories] = useState([]);
  const [quarantine, setQuarantine] = useState([]);
  const [showQuarantine, setShowQuarantine] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [draftContent, setDraftContent] = useState('');
  const [draftCategory, setDraftCategory] = useState('fact');
  const [draftCritical, setDraftCritical] = useState(false);

  const loadMemories = useCallback(async () => {
    const resp = await authFetch('/v1/memories?active=true&limit=200');
    const { consentRequired: needsConsent, data } = await parseMemoryResponse(resp);
    setConsentRequired(needsConsent);
    setMemories(needsConsent ? [] : (Array.isArray(data?.rows) ? data.rows : []));
  }, []);

  const loadQuarantine = useCallback(async () => {
    const resp = await authFetch('/v1/memories/quarantine?limit=100');
    const { consentRequired: needsConsent, data } = await parseMemoryResponse(resp);
    setQuarantine(needsConsent ? [] : (Array.isArray(data?.rows) ? data.rows : []));
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      await Promise.all([loadMemories(), loadQuarantine()]);
    } catch (err) {
      setError(err.message || 'Could not load memories.');
    } finally {
      setLoading(false);
    }
  }, [loadMemories, loadQuarantine]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const createMemory = useCallback(async () => {
    const content = draftContent.trim();
    if (!content) return;
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch('/v1/memories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, category: draftCategory, critical: draftCritical }),
      });
      const { consentRequired: needsConsent } = await parseMemoryResponse(resp);
      setConsentRequired(needsConsent);
      if (!needsConsent) {
        setDraftContent('');
        setDraftCritical(false);
        await loadMemories();
      }
    } catch (err) {
      setError(err.message || 'Could not save the memory.');
    } finally {
      setBusy(false);
    }
  }, [draftContent, draftCategory, draftCritical, loadMemories]);

  const saveMemory = useCallback(async (memoryId, fields) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/v1/memories/${encodePath(memoryId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields),
      });
      await parseMemoryResponse(resp);
      await loadMemories();
    } catch (err) {
      setError(err.message || 'Could not update the memory.');
    } finally {
      setBusy(false);
    }
  }, [loadMemories]);

  const deleteMemory = useCallback(async (memoryId) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/v1/memories/${encodePath(memoryId)}`, { method: 'DELETE' });
      await parseMemoryResponse(resp);
      await loadMemories();
    } catch (err) {
      setError(err.message || 'Could not delete the memory.');
    } finally {
      setBusy(false);
    }
  }, [loadMemories]);

  const restoreQuarantine = useCallback(async (quarantineId) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/v1/memories/quarantine/${encodePath(quarantineId)}/restore`, { method: 'POST' });
      await parseMemoryResponse(resp);
      await Promise.all([loadMemories(), loadQuarantine()]);
    } catch (err) {
      setError(err.message || 'Could not restore the memory.');
    } finally {
      setBusy(false);
    }
  }, [loadMemories, loadQuarantine]);

  const discardQuarantine = useCallback(async (quarantineId) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/v1/memories/quarantine/${encodePath(quarantineId)}`, { method: 'DELETE' });
      await parseMemoryResponse(resp);
      await loadQuarantine();
    } catch (err) {
      setError(err.message || 'Could not discard the quarantined item.');
    } finally {
      setBusy(false);
    }
  }, [loadQuarantine]);

  if (loading) {
    return <EmptyState>Loading your memories...</EmptyState>;
  }

  if (consentRequired) {
    return (
      <EmptyState>
        Turn on Cloud Sync in Settings to store memories with your account. Once it's
        on, anything Viola remembers about you shows up here — scoped to your account
        only, encrypted, and never shared with anyone else.
      </EmptyState>
    );
  }

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      {error && (
        <div style={{ padding: '9px 10px', borderRadius: 6, color: THEME.colors.statusRed, border: `1px solid ${THEME.colors.statusRed}` }}>
          {error}
        </div>
      )}

      <section style={{ display: 'grid', gap: 8 }}>
        <h3 style={{ margin: 0, fontSize: 14, color: THEME.colors.textBright }}>Add a memory</h3>
        <textarea
          value={draftContent}
          onChange={(event) => setDraftContent(event.target.value)}
          placeholder="e.g. I'm allergic to peanuts"
          aria-label="New memory content"
          data-testid="cloud-memory-new-content"
          style={{
            minHeight: 70,
            resize: 'vertical',
            boxSizing: 'border-box',
            width: '100%',
            padding: 10,
            borderRadius: 6,
            border: `1px solid ${THEME.colors.borderLight}`,
            backgroundColor: THEME.colors.glassBase,
            color: THEME.colors.textPrimary,
            fontFamily: PANEL_FONT,
            fontSize: 13,
          }}
        />
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <select
            value={draftCategory}
            onChange={(event) => setDraftCategory(event.target.value)}
            aria-label="New memory category"
            style={{ ...buttonBase, padding: '6px 10px' }}
          >
            {CLOUD_MEMORY_CATEGORIES.map((cat) => (
              <option key={cat} value={cat}>{cat}</option>
            ))}
          </select>
          <label style={{ display: 'flex', alignItems: 'center', gap: 5, color: THEME.colors.textSecondary, fontSize: 12, minHeight: 44 }}>
            <input type="checkbox" checked={draftCritical} onChange={(event) => setDraftCritical(event.target.checked)} />
            Critical
          </label>
          <div style={{ flex: 1 }} />
          <button
            type="button"
            onClick={createMemory}
            disabled={busy || !draftContent.trim()}
            data-testid="cloud-memory-add"
            style={{ ...buttonBase, padding: '7px 14px', backgroundColor: BRONZE, borderColor: BRONZE, color: '#fff', fontWeight: 700 }}
          >
            Add
          </button>
        </div>
      </section>

      <section style={{ display: 'grid', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 }}>
          <h3 style={{ margin: 0, fontSize: 15, color: THEME.colors.textBright }}>Memories</h3>
          <span style={{ color: THEME.colors.textMuted, fontSize: 12 }}>{memories.length} entries</span>
        </div>
        {memories.length ? (
          <div style={{ display: 'grid', gap: 8 }} data-testid="cloud-memory-list">
            {memories.map((memory) => (
              <CloudMemoryRow key={memory.id} memory={memory} busy={busy} onSave={saveMemory} onDelete={deleteMemory} />
            ))}
          </div>
        ) : (
          <EmptyState>No memories yet. Add one above, or just talk to Viola — she saves what she learns here too.</EmptyState>
        )}
      </section>

      <section style={{ display: 'grid', gap: 10 }}>
        <button
          type="button"
          onClick={() => setShowQuarantine((prev) => !prev)}
          style={{ ...buttonBase, padding: '7px 12px', justifySelf: 'start' }}
        >
          {showQuarantine ? 'Hide' : 'Show'} quarantined items ({quarantine.length})
        </button>
        {showQuarantine && (
          quarantine.length ? (
            <div style={{ display: 'grid', gap: 8 }}>
              {quarantine.map((item) => (
                <CloudQuarantineRow key={item.id} item={item} busy={busy} onRestore={restoreQuarantine} onDelete={discardQuarantine} />
              ))}
            </div>
          ) : (
            <EmptyState>Nothing in quarantine. Flagged or conflicting memories would show up here for review.</EmptyState>
          )
        )}
      </section>
    </div>
  );
}

function MemoryPanelContent({ isOpen, onClose }) {
  // The cloud SPA has no 'viola' tab (no VIOLA.md filesystem) -- default straight
  // to the Memory tab, which is cloud-native there (#1182).
  const [activeTab, setActiveTab] = useState(() => (isCloudSurface() ? 'memory' : 'viola'));
  const [violaContent, setViolaContent] = useState('');
  const [violaDraft, setViolaDraft] = useState('');
  const [editingViola, setEditingViola] = useState(false);
  const [memoryEntries, setMemoryEntries] = useState({ index: { entries: [] }, topics: [] });
  const [expandedTopics, setExpandedTopics] = useState({});
  const [workbenchFiles, setWorkbenchFiles] = useState([]);
  const [dropActive, setDropActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef(null);

  const loadViola = useCallback(async () => {
    // VIOLA.md + entries read the desktop's local D-layout memory files --
    // desktop filesystem only, no cloud equivalent (#1182: the cloud SPA's
    // Memory tab is now cloud-native via CloudMemoryTab above, but VIOLA.md
    // itself has nothing to rewire to). Skip the dead fetch on the cloud SPA.
    if (isCloudSurface()) return;
    const resp = await authFetch('/api/memory/viola');
    const data = await parseResponse(resp);
    const content = data?.content || '';
    setViolaContent(content);
    setViolaDraft(content);
  }, []);

  const loadMemoryEntries = useCallback(async () => {
    // Same desktop-only D-layout file source as loadViola above; the cloud
    // SPA's Memory tab renders CloudMemoryTab instead (#1182), which does its
    // own /v1/memories fetching, so this desktop-file loader stays skipped.
    if (isCloudSurface()) return;
    const resp = await authFetch('/api/memory/entries');
    const data = await parseResponse(resp);
    setMemoryEntries({
      index: data?.index || { entries: [] },
      topics: Array.isArray(data?.topics) ? data.topics : [],
    });
  }, []);

  const loadWorkbench = useCallback(async () => {
    // Workbench files live on the desktop's local disk -- desktop-only
    // (#1064). Skip the dead fetch on the cloud SPA; the Workbench tab
    // renders a DesktopUpsell.
    if (isFeatureHidden('workbench')) return;
    const resp = await authFetch('/api/workbench/files');
    const data = await parseResponse(resp);
    setWorkbenchFiles(Array.isArray(data?.files) ? data.files : []);
  }, []);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      await Promise.all([loadViola(), loadMemoryEntries(), loadWorkbench()]);
    } catch (err) {
      setError(err.message || 'Could not load memory.');
    } finally {
      setBusy(false);
    }
  }, [loadMemoryEntries, loadViola, loadWorkbench]);

  useEffect(() => {
    if (!isOpen) return undefined;
    refresh();
    return undefined;
  }, [isOpen, refresh]);

  const saveViola = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch('/api/memory/viola', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: violaDraft }),
      });
      await parseResponse(resp);
      setViolaContent(violaDraft);
      setEditingViola(false);
      await loadMemoryEntries();
    } catch (err) {
      setError(err.message || 'Could not save VIOLA.md.');
    } finally {
      setBusy(false);
    }
  }, [loadMemoryEntries, violaDraft]);

  const insertTemplate = useCallback((template) => {
    setEditingViola(true);
    setViolaDraft((prev) => `${prev}${prev && !prev.endsWith('\n') ? '\n' : ''}${template.trimStart()}`);
  }, []);

  const deleteIndexEntry = useCallback(async (entry) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/api/memory/entries/${encodePath(entry.id)}`, { method: 'DELETE' });
      await parseResponse(resp);
      await loadMemoryEntries();
    } catch (err) {
      setError(err.message || 'Could not delete memory item.');
    } finally {
      setBusy(false);
    }
  }, [loadMemoryEntries]);

  const toggleTopic = useCallback((name) => {
    setExpandedTopics((prev) => ({ ...prev, [name]: !prev[name] }));
  }, []);

  const deleteTopicFile = useCallback(async (topic) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/api/memory/topics/${encodePath(topic.filename || topic.name)}`, { method: 'DELETE' });
      await parseResponse(resp);
      await loadMemoryEntries();
    } catch (err) {
      setError(err.message || 'Could not delete topic file.');
    } finally {
      setBusy(false);
    }
  }, [loadMemoryEntries]);

  const deleteTopicEntry = useCallback(async (topic, entry) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/api/memory/topics/${encodePath(topic.filename || topic.name)}/entries/${encodePath(entry.id)}`, { method: 'DELETE' });
      await parseResponse(resp);
      await loadMemoryEntries();
    } catch (err) {
      setError(err.message || 'Could not delete topic entry.');
    } finally {
      setBusy(false);
    }
  }, [loadMemoryEntries]);

  const uploadFiles = useCallback(async (fileList) => {
    const selected = Array.from(fileList || []);
    if (!selected.length) return;
    setBusy(true);
    setError('');
    try {
      for (const file of selected) {
        const form = new FormData();
        form.append('file', file, file.name);
        const resp = await authFetch('/api/workbench/files', { method: 'POST', body: form });
        await parseResponse(resp);
      }
      await loadWorkbench();
    } catch (err) {
      setError(err.message || 'Could not upload file.');
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  }, [loadWorkbench]);

  const deleteWorkbenchFile = useCallback(async (name) => {
    setBusy(true);
    setError('');
    try {
      const resp = await authFetch(`/api/workbench/files/${encodePath(name)}`, { method: 'DELETE' });
      await parseResponse(resp);
      await loadWorkbench();
    } catch (err) {
      setError(err.message || 'Could not delete file.');
    } finally {
      setBusy(false);
    }
  }, [loadWorkbench]);

  const openWorkbenchFolder = useCallback(async () => {
    try {
      const resp = await authFetch('/api/workbench/open-folder');
      await parseResponse(resp);
    } catch (err) {
      setError(err.message || 'Could not open Workbench folder.');
    }
  }, []);

  const tabContent = useMemo(() => {
    if (activeTab === 'workbench' && isFeatureHidden('workbench')) {
      return (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '24px' }}>
          <DesktopUpsell feature="workbench" />
        </div>
      );
    }
    if (activeTab === 'memory' && isCloudSurface()) {
      // Cloud-native Memory tab (#1182): real per-user rows via
      // /v1/memories/*, not the desktop-file MemoryTab below. Owns its own
      // data fetching (including the consent-required empty state), so no
      // DesktopUpsell and no dependency on this memo's desktop-file state.
      return <CloudMemoryTab />;
    }
    if (activeTab === 'viola') {
      return (
        <ViolaTab
          content={violaContent}
          draft={violaDraft}
          editing={editingViola}
          busy={busy}
          onDraftChange={setViolaDraft}
          onEdit={() => setEditingViola(true)}
          onCancel={() => { setViolaDraft(violaContent); setEditingViola(false); }}
          onSave={saveViola}
          onInsertTemplate={insertTemplate}
        />
      );
    }
    if (activeTab === 'memory') {
      return (
        <MemoryTab
          entries={memoryEntries}
          busy={busy}
          expandedTopics={expandedTopics}
          onDeleteIndexEntry={deleteIndexEntry}
          onToggleTopic={toggleTopic}
          onDeleteTopicFile={deleteTopicFile}
          onDeleteTopicEntry={deleteTopicEntry}
        />
      );
    }
    return (
      <WorkbenchTab
        files={workbenchFiles}
        busy={busy}
        dropActive={dropActive}
        inputRef={inputRef}
        onUpload={uploadFiles}
        onDelete={deleteWorkbenchFile}
        onOpenFolder={openWorkbenchFolder}
        onDropActive={setDropActive}
      />
    );
  }, [
    activeTab,
    busy,
    deleteIndexEntry,
    deleteTopicEntry,
    deleteTopicFile,
    deleteWorkbenchFile,
    dropActive,
    editingViola,
    expandedTopics,
    insertTemplate,
    memoryEntries,
    openWorkbenchFolder,
    saveViola,
    toggleTopic,
    uploadFiles,
    violaContent,
    violaDraft,
    workbenchFiles,
  ]);

  // No 'viola' tab on the cloud SPA (no VIOLA.md filesystem there) -- see the
  // activeTab default above and the DESKTOP_TABS/CLOUD_TABS split (#1182).
  const tabs = useMemo(() => (isCloudSurface() ? CLOUD_TABS : DESKTOP_TABS), []);

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Memory and Workbench"
      data-testid="memory-panel"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 10030,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: THEME.colors.overlay,
        fontFamily: PANEL_FONT,
      }}
      onClick={onClose}
    >
      <div
        onClick={(event) => event.stopPropagation()}
        style={{
          width: 'min(900px, calc(100vw - 28px))',
          maxHeight: '86vh',
          minHeight: '540px',
          display: 'flex',
          flexDirection: 'column',
          borderRadius: 8,
          border: `1px solid ${THEME.colors.borderHover}`,
          backgroundColor: THEME.colors.bgElevated,
          boxShadow: `0 24px 72px ${THEME.colors.shadowDeep}`,
          color: THEME.colors.textPrimary,
          overflow: 'hidden',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '16px 18px', borderBottom: `1px solid ${THEME.colors.divider}` }}>
          <div style={{ width: 4, height: 26, borderRadius: 2, backgroundColor: BRONZE }} />
          <div style={{ flex: 1, minWidth: 0, fontSize: 18, fontWeight: 700 }}>
            {tabs.find((tab) => tab.id === activeTab)?.label || 'Memory'}
          </div>
          <button type="button" onClick={onClose} aria-label="Close" style={{ ...buttonBase, width: 44, padding: 0 }}>X</button>
        </div>

        <div style={{ display: 'flex', gap: 6, padding: '10px 18px', borderBottom: `1px solid ${THEME.colors.divider}` }}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              style={{
                ...buttonBase,
                padding: '7px 12px',
                borderColor: activeTab === tab.id ? BRONZE : THEME.colors.borderLight,
                backgroundColor: activeTab === tab.id ? BRONZE_FILL_MED : 'transparent',
                color: activeTab === tab.id ? THEME.colors.textBright : THEME.colors.textSecondary,
                fontWeight: activeTab === tab.id ? 700 : 500,
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {error && (
          <div style={{ margin: '12px 18px 0', padding: '9px 10px', borderRadius: 6, color: THEME.colors.statusRed, border: `1px solid ${THEME.colors.statusRed}` }}>
            {error}
          </div>
        )}

        <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 18 }}>
          {tabContent}
        </div>
      </div>
    </div>
  );
}

MemoryPanelContent.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
};

export default function MemoryPanel(props) {
  return (
    <ErrorBoundary name="Memory panel">
      <MemoryPanelContent {...props} />
    </ErrorBoundary>
  );
}
