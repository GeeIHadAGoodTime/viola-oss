import React, { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { CloseButton } from '../settings';

const theme = THEME;

function fieldStyle() {
  return {
    width: '100%',
    padding: '10px 12px',
    borderRadius: '8px',
    border: `1px solid ${theme.colors.borderLight}`,
    backgroundColor: theme.colors.bgCard,
    color: theme.colors.textPrimary,
    fontSize: '13px',
    outline: 'none',
    boxSizing: 'border-box',
  };
}

function buttonStyle({ primary = false } = {}) {
  return {
    padding: '10px 18px',
    borderRadius: '10px',
    border: primary ? 'none' : `1px solid ${theme.colors.borderLight}`,
    backgroundColor: primary ? theme.colors.accent : 'transparent',
    color: primary ? '#fff' : theme.colors.textSecondary,
    fontSize: '14px',
    fontWeight: primary ? 600 : 500,
    cursor: 'pointer',
  };
}

function parseEnvVars(text) {
  const env = {};
  text.split('\n').forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) return;
    const idx = trimmed.indexOf('=');
    if (idx <= 0) return;
    env[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  });
  return env;
}

const RegisterMCPForm = React.memo(({ entry, isOpen, onClose, onSubmit }) => {
  const [name, setName] = useState(entry?.id || entry?.name || '');
  const [command, setCommand] = useState(entry?.command || '');
  const [argsText, setArgsText] = useState((entry?.args || []).join('\n'));
  const [envText, setEnvText] = useState((entry?.env_vars_required || []).map((key) => `${key}=`).join('\n'));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const titleId = React.useId();

  React.useEffect(() => {
    if (!entry) return;
    setName(entry.id || entry.name || '');
    setCommand(entry.command || '');
    setArgsText((entry.args || []).join('\n'));
    setEnvText((entry.env_vars_required || []).map((key) => `${key}=`).join('\n'));
    setError('');
  }, [entry]);

  const args = useMemo(() => argsText.split('\n').map((arg) => arg.trim()).filter(Boolean), [argsText]);

  if (!isOpen) {
    return null;
  }

  const submit = async (event) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await onSubmit({
        name: name.trim(),
        command: command.trim(),
        args,
        env: parseEnvVars(envText),
      });
      onClose();
    } catch (err) {
      // Registration failed — keep the modal open (so the user can fix the
      // command and retry) and surface the reason IN the modal. The parent's
      // status banner sits behind this overlay and would never be seen; before
      // this, a failed register left the form open with no message at all and
      // the rejection became an unhandled promise rejection.
      setError(err?.message || 'Could not register the MCP server. Check the command and try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      role="presentation"
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1300,
        backgroundColor: theme.colors.overlay,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '20px',
        boxSizing: 'border-box',
      }}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        style={{
          width: 'min(560px, 100%)',
          backgroundColor: theme.colors.bgCard,
          border: `1px solid ${theme.colors.borderLight}`,
          borderRadius: '16px',
          boxShadow: `0 24px 80px ${theme.colors.shadowDeep}`,
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '16px 18px', borderBottom: `1px solid ${theme.colors.borderLight}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 id={titleId} style={{ margin: 0, color: theme.colors.textPrimary, fontSize: '16px' }}>Register MCP Server</h3>
          <CloseButton onClick={onClose} />
        </div>
        <div style={{ padding: '18px', display: 'grid', gap: '14px' }}>
          <label style={{ color: theme.colors.textSecondary, fontSize: '13px', display: 'grid', gap: '6px' }}>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required style={fieldStyle()} />
          </label>
          <label style={{ color: theme.colors.textSecondary, fontSize: '13px', display: 'grid', gap: '6px' }}>
            Command
            <input value={command} onChange={(e) => setCommand(e.target.value)} required style={fieldStyle()} />
          </label>
          <label style={{ color: theme.colors.textSecondary, fontSize: '13px', display: 'grid', gap: '6px' }}>
            Args
            <textarea rows={5} value={argsText} onChange={(e) => setArgsText(e.target.value)} style={{ ...fieldStyle(), resize: 'vertical' }} />
          </label>
          <label style={{ color: theme.colors.textSecondary, fontSize: '13px', display: 'grid', gap: '6px' }}>
            Environment variables
            <textarea rows={4} value={envText} onChange={(e) => setEnvText(e.target.value)} style={{ ...fieldStyle(), resize: 'vertical' }} />
          </label>
        </div>
        {error && (
          <div
            role="alert"
            style={{ padding: '0 18px 4px', color: theme.colors.statusRed, fontSize: '12px' }}
          >
            {error}
          </div>
        )}
        <div style={{ padding: '14px 18px', borderTop: `1px solid ${theme.colors.borderLight}`, display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
          <button type="button" onClick={onClose} style={buttonStyle()}>Cancel</button>
          <button type="submit" disabled={submitting} style={buttonStyle({ primary: true })}>
            {submitting ? 'Registering...' : 'Register'}
          </button>
        </div>
      </form>
    </div>
  );
});

RegisterMCPForm.propTypes = {
  entry: PropTypes.object,
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  onSubmit: PropTypes.func.isRequired,
};

export default RegisterMCPForm;
