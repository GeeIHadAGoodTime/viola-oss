/* eslint react/jsx-uses-vars: "error" */
import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { authFetch } from '../hooks/useViolaApi';
import WorkbenchKindIcon from './WorkbenchKindIcon';
import WorkbenchToast from './WorkbenchToast';

const MAX_FILE_BYTES = 25 * 1024 * 1024;
const OVERLAY_KINDS = ['resume', 'id_card', 'insurance', 'recipe', 'photo', 'receipt', 'doc'];

let nextWorkbenchToastId = 1;

function hasFiles(event) {
  const types = Array.from(event.dataTransfer?.types || []);
  return types.includes('Files');
}

async function parseWorkbenchResponse(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const message = payload?.error?.message || payload?.message || `Workbench upload failed (${response.status})`;
    throw new Error(message);
  }
  if (payload && typeof payload === 'object' && payload.data !== undefined && payload.ok !== false) {
    return payload.data;
  }
  return payload;
}

export default function WorkbenchDropZone({ addToast }) {
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [workbenchToasts, setWorkbenchToasts] = useState([]);
  const dragDepthRef = useRef(0);

  const showError = useCallback((message) => {
    if (!addToast) return;
    addToast({ message, level: 'error' });
  }, [addToast]);

  const showWarning = useCallback((message) => {
    if (!addToast) return;
    addToast({ message, level: 'warning' });
  }, [addToast]);

  const addWorkbenchToast = useCallback((toast) => {
    const id = nextWorkbenchToastId++;
    setWorkbenchToasts(prev => [...prev, { id, ...toast }]);
    return id;
  }, []);

  const dismissWorkbenchToast = useCallback((id) => {
    setWorkbenchToasts(prev => prev.filter(toast => toast.id !== id));
  }, []);

  const [clashPrompt, setClashPrompt] = useState(null);

  const uploadFile = useCallback(async (file, options = {}) => {
    const formData = new FormData();
    formData.append('file', file, file.name);
    if (options.onFilenameClash) {
      formData.append('on_filename_clash', options.onFilenameClash);
    }
    const response = await authFetch('/v1/knowledge', {
      method: 'POST',
      body: formData,
    });
    return parseWorkbenchResponse(response);
  }, []);

  const handleClashChoice = useCallback(async (choice) => {
    const prompt = clashPrompt;
    if (!prompt) return;
    setClashPrompt(null);
    if (choice === 'cancel') return;
    setUploading(true);
    try {
      const result = await uploadFile(prompt.file, { onFilenameClash: choice });
      if (result?.action_required === 'filename_clash') {
        setClashPrompt({ file: prompt.file, existing: result.existing });
      } else {
        addWorkbenchToast({
          type: result?.replaced_id || result?.superseded_id || result?.superseded_item_id ? 'supersede' : 'ingest',
          result,
          originalFile: prompt.file,
        });
      }
    } catch (error) {
      showError(error.message || `Could not remember ${prompt.file.name}.`);
    } finally {
      setUploading(false);
    }
  }, [addWorkbenchToast, clashPrompt, showError, uploadFile]);

  useEffect(() => {
    const body = document.body;
    if (!body) return undefined;

    const cancel = (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = uploading ? 'none' : 'copy';
      }
    };

    const handleDragEnter = (event) => {
      if (!hasFiles(event)) return;
      cancel(event);
      dragDepthRef.current += 1;
      setDragActive(true);
    };

    const handleDragOver = (event) => {
      cancel(event);
    };

    const handleDragLeave = (event) => {
      if (!hasFiles(event)) return;
      cancel(event);
      dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
      if (dragDepthRef.current === 0) {
        setDragActive(false);
      }
    };

    const handleDrop = async (event) => {
      if (!hasFiles(event)) return;
      cancel(event);
      dragDepthRef.current = 0;
      setDragActive(false);

      const files = Array.from(event.dataTransfer?.files || []);
      if (files.length === 0) return;

      setUploading(true);
      try {
        for (const file of files) {
          if (file.size > MAX_FILE_BYTES) {
            showWarning(`${file.name} too large (max 25 MB)`);
            continue;
          }
          try {
            const result = await uploadFile(file);
            if (result?.action_required === 'filename_clash') {
              setClashPrompt({ file, existing: result.existing });
              continue;
            }
            addWorkbenchToast({
              type: result?.replaced_id || result?.superseded_id || result?.superseded_item_id ? 'supersede' : 'ingest',
              result,
              originalFile: file,
            });
          } catch (error) {
            showError(error.message || `Could not remember ${file.name}.`);
          }
        }
      } finally {
        setUploading(false);
      }
    };

    body.addEventListener('dragenter', handleDragEnter);
    body.addEventListener('dragover', handleDragOver);
    body.addEventListener('dragleave', handleDragLeave);
    body.addEventListener('drop', handleDrop);

    return () => {
      body.removeEventListener('dragenter', handleDragEnter);
      body.removeEventListener('dragover', handleDragOver);
      body.removeEventListener('dragleave', handleDragLeave);
      body.removeEventListener('drop', handleDrop);
    };
  }, [addWorkbenchToast, showError, showWarning, uploadFile, uploading]);

  // Desktop QWebEngineView path: Qt intercepts file drops at the widget level
  // (because Chromium's drop forwarding is unreliable in embedded contexts —
  // it routes file drops through createWindow() which opens the OAuth popup
  // instead of dispatching to JS). The Qt subclass posts to /v1/knowledge
  // itself and dispatches legacy custom DOM events that we surface here.
  useEffect(() => {
    const onDragStart = () => {
      dragDepthRef.current = 1;
      setDragActive(true);
    };
    const onDragEnd = () => {
      dragDepthRef.current = 0;
      setDragActive(false);
    };
    const onUploading = () => {
      setUploading(true);
    };
    const onResult = (event) => {
      setUploading(false);
      const detail = event?.detail || {};
      if (detail.ok && detail.result) {
        const result = detail.result;
        addWorkbenchToast({
          type: result.superseded_id || result.superseded_item_id ? 'supersede' : 'ingest',
          result,
          originalFile: { name: result.filename || 'file' },
        });
      } else {
        showError(detail.error || 'Could not remember the file.');
      }
    };

    window.addEventListener('viola:knowledge-drag-start', onDragStart);
    window.addEventListener('viola:knowledge-drag-end', onDragEnd);
    window.addEventListener('viola:knowledge-uploading', onUploading);
    window.addEventListener('viola:knowledge-ingest-result', onResult);
    return () => {
      window.removeEventListener('viola:knowledge-drag-start', onDragStart);
      window.removeEventListener('viola:knowledge-drag-end', onDragEnd);
      window.removeEventListener('viola:knowledge-uploading', onUploading);
      window.removeEventListener('viola:knowledge-ingest-result', onResult);
    };
  }, [addWorkbenchToast, showError]);

  return (
    <>
      {(dragActive || uploading) && (
        <div
          role="region"
          aria-label="Workbench ingest"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 10010,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            backgroundColor: THEME.colors.overlay,
            color: THEME.colors.textPrimary,
            pointerEvents: 'none',
          }}
        >
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: '18px',
              padding: '28px 34px',
              borderRadius: '8px',
              border: `1px solid ${THEME.colors.borderHover}`,
              backgroundColor: THEME.colors.bgElevated,
              boxShadow: `0 24px 80px ${THEME.colors.shadowDeep}`,
            }}
          >
            <div style={{ fontSize: '22px', fontWeight: 700, color: THEME.colors.textBright }}>
              {uploading ? 'Saving in Workbench...' : 'Drop to save in Workbench'}
            </div>
            <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
              {OVERLAY_KINDS.map(kind => (
                <div
                  key={kind}
                  style={{
                    width: '36px',
                    height: '36px',
                    borderRadius: '8px',
                    border: `1px solid ${THEME.colors.borderLight}`,
                    backgroundColor: THEME.colors.glassBase,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <WorkbenchKindIcon kind={kind} size={22} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <WorkbenchToast
        toasts={workbenchToasts}
        onDismiss={dismissWorkbenchToast}
        onError={showError}
      />

      {clashPrompt && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Existing file"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 10025,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            backgroundColor: THEME.colors.overlay,
          }}
          onClick={() => handleClashChoice('cancel')}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: 'min(440px, calc(100vw - 32px))',
              padding: '20px 22px',
              borderRadius: 8,
              backgroundColor: THEME.colors.bgElevated,
              border: `1px solid ${THEME.colors.borderHover}`,
              boxShadow: `0 24px 64px ${THEME.colors.shadowDeep}`,
              color: THEME.colors.textPrimary,
            }}
          >
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>
              You already have {clashPrompt.existing?.filename || clashPrompt.file?.name || 'this file'}.
            </div>
            <div style={{ fontSize: 13, color: THEME.colors.textMuted, marginBottom: 16 }}>
              {clashPrompt.existing?.size_label
                ? `Existing: ${clashPrompt.existing.size_label}`
                : 'It was saved earlier.'}
              {' '}Replace it with the new copy, or keep both?
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                onClick={() => handleClashChoice('cancel')}
                style={{
                  padding: '8px 14px',
                  borderRadius: 6,
                  border: `1px solid ${THEME.colors.borderLight}`,
                  backgroundColor: 'transparent',
                  color: THEME.colors.textMuted,
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => handleClashChoice('keep_both')}
                style={{
                  padding: '8px 14px',
                  borderRadius: 6,
                  border: `1px solid ${THEME.colors.borderLight}`,
                  backgroundColor: 'transparent',
                  color: THEME.colors.textPrimary,
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                Keep both
              </button>
              <button
                type="button"
                onClick={() => handleClashChoice('replace')}
                style={{
                  padding: '8px 14px',
                  borderRadius: 6,
                  border: `1px solid ${THEME.colors.accent}`,
                  backgroundColor: THEME.colors.accent,
                  color: '#fff',
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                Replace
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

WorkbenchDropZone.propTypes = {
  addToast: PropTypes.func,
};
