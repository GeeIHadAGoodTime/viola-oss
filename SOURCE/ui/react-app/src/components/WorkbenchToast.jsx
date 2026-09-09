import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { authFetch } from '../hooks/useViolaApi';
import WorkbenchKindIcon from './WorkbenchKindIcon';

const DEFAULT_DISMISS_MS = 6000;
const UNDO_DISMISS_MS = 12000;

async function parseResponse(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const message = payload?.error?.message || payload?.message || `Request failed (${response.status})`;
    throw new Error(message);
  }
  if (payload && typeof payload === 'object' && 'data' in payload && payload.ok !== false) {
    return payload.data;
  }
  return payload;
}

function getItemId(result) {
  if (!result) return '';
  return result.item_id || result.id || '';
}

function getFilename(result) {
  if (!result) return 'file';
  return result.filename || result.title || 'file';
}

function getSizeLabel(result) {
  if (!result) return '';
  if (result.size_label) return result.size_label;
  const bytes = Number(result.byte_size || 0);
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function detectKindFromFilename(filename) {
  if (!filename) return 'doc';
  const ext = filename.toLowerCase().split('.').pop();
  if (['pdf', 'doc', 'docx', 'md', 'txt', 'rtf'].includes(ext)) return 'doc';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'heic'].includes(ext)) return 'photo';
  if (['html', 'htm'].includes(ext)) return 'webpage';
  return 'doc';
}

function WorkbenchToastItem({ toast, onDismiss, onError }) {
  const result = useMemo(() => toast.result || {}, [toast.result]);
  const filename = getFilename(result);
  const sizeLabel = getSizeLabel(result);
  const kindIcon = detectKindFromFilename(filename);
  const isUndo = toast.type === 'undo';
  const dismissMs = isUndo ? UNDO_DISMISS_MS : (toast.dismissMs || DEFAULT_DISMISS_MS);

  const [paused, setPaused] = useState(false);
  const [busy, setBusy] = useState(false);
  const timerRef = useRef(null);

  const dismiss = useCallback(() => {
    onDismiss(toast.id);
  }, [onDismiss, toast.id]);

  useEffect(() => {
    if (paused || busy) return undefined;
    timerRef.current = setTimeout(dismiss, dismissMs);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [busy, dismiss, dismissMs, paused]);

  const handleForget = useCallback(async () => {
    const itemId = getItemId(result);
    if (!itemId) {
      dismiss();
      return;
    }
    setBusy(true);
    try {
      const response = await authFetch(`/v1/knowledge/${encodeURIComponent(itemId)}`, { method: 'DELETE' });
      await parseResponse(response);
      dismiss();
    } catch (error) {
      onError(error.message || `Could not forget ${filename}.`);
    } finally {
      setBusy(false);
    }
  }, [dismiss, filename, onError, result]);

  const titleText = isUndo ? (toast.title || 'Forgotten.') : `Saved ${filename}`;
  const subtitleText = isUndo
    ? (toast.summary || 'Use Workbench to recover.')
    : (sizeLabel || 'in your Workbench');

  return (
    <div
      role="status"
      aria-live="polite"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      style={{
        display: 'grid',
        gridTemplateColumns: '32px minmax(0, 1fr) auto',
        alignItems: 'center',
        gap: '12px',
        width: 'min(420px, calc(100vw - 32px))',
        padding: '12px 14px',
        borderRadius: '8px',
        border: `1px solid ${THEME.colors.borderLight}`,
        backgroundColor: THEME.colors.bgElevated,
        boxShadow: `0 18px 50px ${THEME.colors.shadowDeep}`,
        color: THEME.colors.textPrimary,
        pointerEvents: 'auto',
      }}
    >
      <div
        style={{
          width: '32px',
          height: '32px',
          borderRadius: '8px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          backgroundColor: THEME.colors.glassBase,
        }}
      >
        <WorkbenchKindIcon kind={kindIcon} size={20} />
      </div>
      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
        <div
          style={{
            fontSize: '14px',
            fontWeight: 700,
            color: THEME.colors.textBright,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {titleText}
        </div>
        <div style={{ fontSize: '12px', color: THEME.colors.textMuted }}>{subtitleText}</div>
      </div>
      <div style={{ display: 'flex', gap: '6px' }}>
        <button
          type="button"
          aria-label="Delete"
          onClick={handleForget}
          disabled={busy}
          style={{
            padding: '6px 10px',
            borderRadius: '6px',
            border: `1px solid ${THEME.colors.borderLight}`,
            backgroundColor: 'transparent',
            color: THEME.colors.textMuted,
            fontSize: '12px',
            cursor: busy ? 'wait' : 'pointer',
          }}
        >
          Delete
        </button>
      </div>
    </div>
  );
}

WorkbenchToastItem.propTypes = {
  toast: PropTypes.shape({
    id: PropTypes.number.isRequired,
    type: PropTypes.oneOf(['ingest', 'supersede', 'undo']),
    result: PropTypes.object,
    title: PropTypes.string,
    summary: PropTypes.string,
    dismissMs: PropTypes.number,
  }).isRequired,
  onDismiss: PropTypes.func.isRequired,
  onError: PropTypes.func.isRequired,
};

export default function WorkbenchToast({ toasts, onDismiss, onError }) {
  const visibleToasts = (toasts || []).slice(-3);
  if (visibleToasts.length === 0) return null;
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      style={{
        position: 'fixed',
        right: '18px',
        bottom: '18px',
        zIndex: 10020,
        display: 'flex',
        flexDirection: 'column-reverse',
        gap: '10px',
        pointerEvents: 'none',
      }}
    >
      {visibleToasts.map(toast => (
        <WorkbenchToastItem
          key={toast.id}
          toast={toast}
          onDismiss={onDismiss}
          onError={onError}
        />
      ))}
    </div>
  );
}

WorkbenchToast.propTypes = {
  toasts: PropTypes.arrayOf(PropTypes.shape({
    id: PropTypes.number.isRequired,
    type: PropTypes.oneOf(['ingest', 'supersede', 'undo']),
    result: PropTypes.object,
    title: PropTypes.string,
    summary: PropTypes.string,
    dismissMs: PropTypes.number,
  })).isRequired,
  onDismiss: PropTypes.func.isRequired,
  onError: PropTypes.func.isRequired,
};
