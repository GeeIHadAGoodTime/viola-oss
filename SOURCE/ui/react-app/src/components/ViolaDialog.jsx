import { useEffect, useId, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import PropTypes from 'prop-types';
import { THEME } from '../config';

/**
 * Viola's own confirm and text-entry dialogs.
 *
 * `window.confirm` and `window.prompt` showed a raw "Javascript Confirm -
 * http://localhost:8756/..." box, and in Qt WebEngine they are a native modal
 * loop nested inside Chromium: the window stopped repainting after Escape on
 * one (1.0.5 build 9). The Qt shell now refuses them, so everything asks here.
 *
 *   const ok = await confirmDialog({ title, body, confirmLabel, danger });
 *   const text = await promptDialog({ title, label, defaultValue, multiline });
 *
 * Both are plain promises, so a handler reads as it did before. Cancel, Escape
 * and a click on the backdrop answer false (confirm) or null (prompt).
 */

let root = null;
let container = null;
let active = null;
const queue = [];

function render() {
  if (!root) {
    container = document.createElement('div');
    container.dataset.testid = 'viola-dialog-root';
    document.body.appendChild(container);
    root = createRoot(container);
  }
  root.render(active ? <ViolaDialog key={active.id} request={active} onAnswer={answer} /> : null);
}

function answer(value) {
  const finished = active;
  active = queue.shift() || null;
  render();
  finished?.resolve(value);
}

let nextId = 0;
function ask(request) {
  return new Promise((resolve) => {
    nextId += 1;
    const entry = { ...request, id: nextId, resolve };
    if (active) {
      queue.push(entry);
    } else {
      active = entry;
      render();
    }
  });
}

export function confirmDialog({ title, body = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false }) {
  return ask({ kind: 'confirm', title, body, confirmLabel, cancelLabel, danger }).then(Boolean);
}

export function promptDialog({
  title,
  label = '',
  defaultValue = '',
  confirmLabel = 'OK',
  cancelLabel = 'Cancel',
  multiline = false,
}) {
  return ask({ kind: 'prompt', title, label, defaultValue, confirmLabel, cancelLabel, multiline });
}

/** Close whatever is open as if cancelled. Tests call this between cases. */
export function dismissAllDialogs() {
  const open = [active, ...queue].filter(Boolean);
  active = null;
  queue.length = 0;
  if (root) render();
  open.forEach((entry) => entry.resolve(entry.kind === 'prompt' ? null : false));
}

function ViolaDialog({ request, onAnswer }) {
  const isPrompt = request.kind === 'prompt';
  const [value, setValue] = useState(request.defaultValue || '');
  const titleId = useId();
  const bodyId = useId();
  const panelRef = useRef(null);
  const fieldRef = useRef(null);
  const cancelRef = useRef(null);
  const confirmRef = useRef(null);
  const cancelValue = isPrompt ? null : false;

  const submit = () => onAnswer(isPrompt ? value : true);

  useEffect(() => {
    const previous = document.activeElement;
    // A destructive confirm starts on Cancel, so Enter cannot delete by accident.
    const start = isPrompt ? fieldRef.current : (request.danger ? cancelRef.current : confirmRef.current);
    start?.focus();
    if (isPrompt) fieldRef.current?.select?.();
    return () => { if (previous?.isConnected) previous.focus?.(); };
  }, [isPrompt, request.danger]);

  useEffect(() => {
    // Capture on window: this dialog is the top layer, so its Escape must not
    // also close the Settings page or a stage underneath.
    const handleKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        onAnswer(cancelValue);
        return;
      }
      if (event.key === 'Tab') {
        const panel = panelRef.current;
        if (!panel) return;
        const focusable = panel.querySelectorAll('button:not([disabled]), input, textarea');
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener('keydown', handleKey, true);
    return () => window.removeEventListener('keydown', handleKey, true);
  }, [cancelValue, onAnswer]);

  const fieldStyle = {
    width: '100%',
    boxSizing: 'border-box',
    marginTop: '8px',
    padding: '10px 12px',
    borderRadius: '10px',
    border: `1px solid ${THEME.colors.borderLight}`,
    backgroundColor: THEME.colors.bgPrimary,
    color: THEME.colors.textPrimary,
    font: 'inherit',
    resize: 'vertical',
  };
  const buttonStyle = {
    minHeight: '44px',
    minWidth: '88px',
    padding: '0 18px',
    borderRadius: '10px',
    border: `1px solid ${THEME.colors.borderLight}`,
    backgroundColor: 'transparent',
    color: THEME.colors.textPrimary,
    font: 'inherit',
    cursor: 'pointer',
  };

  return (
    <div
      data-testid="viola-dialog-backdrop"
      onPointerDown={(event) => { if (event.target === event.currentTarget) onAnswer(cancelValue); }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 20000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
        backgroundColor: 'rgba(0,0,0,0.55)',
      }}
    >
      <div
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={request.body ? bodyId : undefined}
        style={{
          width: '100%',
          maxWidth: isPrompt && request.multiline ? '560px' : '420px',
          padding: '22px',
          borderRadius: '16px',
          border: `1px solid ${THEME.colors.borderLight}`,
          backgroundColor: THEME.colors.bgElevated,
          color: THEME.colors.textPrimary,
          boxShadow: `0 20px 60px ${THEME.colors.shadowDeep}`,
        }}
      >
        <h2 id={titleId} style={{ margin: 0, fontSize: '17px', fontWeight: 600, overflowWrap: 'anywhere' }}>{request.title}</h2>
        {request.body && (
          <p id={bodyId} style={{ margin: '10px 0 0', color: THEME.colors.textSecondary, lineHeight: 1.5 }}>{request.body}</p>
        )}
        {isPrompt && (
          <label style={{ display: 'block', marginTop: '12px', color: THEME.colors.textSecondary, fontSize: '14px' }}>
            {request.label}
            {request.multiline ? (
              <textarea
                ref={fieldRef}
                rows={5}
                value={value}
                onChange={(event) => setValue(event.target.value)}
                style={fieldStyle}
              />
            ) : (
              <input
                ref={fieldRef}
                value={value}
                onChange={(event) => setValue(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); submit(); } }}
                style={fieldStyle}
              />
            )}
          </label>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '20px' }}>
          <button ref={cancelRef} type="button" onClick={() => onAnswer(cancelValue)} style={buttonStyle}>
            {request.cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={submit}
            style={{
              ...buttonStyle,
              border: 'none',
              backgroundColor: request.danger ? THEME.colors.statusRed : THEME.colors.accent,
              color: '#ffffff',
            }}
          >
            {request.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

ViolaDialog.propTypes = {
  request: PropTypes.shape({
    kind: PropTypes.oneOf(['confirm', 'prompt']).isRequired,
    title: PropTypes.string.isRequired,
    body: PropTypes.string,
    label: PropTypes.string,
    defaultValue: PropTypes.string,
    confirmLabel: PropTypes.string,
    cancelLabel: PropTypes.string,
    danger: PropTypes.bool,
    multiline: PropTypes.bool,
  }).isRequired,
  onAnswer: PropTypes.func.isRequired,
};
