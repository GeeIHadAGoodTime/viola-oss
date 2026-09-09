/**
 * AuthField — labelled text input for the auth screens.
 *
 * Wraps a native <input> with a <label> bound by id, focus-ring styling, and
 * an optional per-field error message. Used by every auth screen so the
 * fields look and behave identically.
 */
import React, { useId, useState } from 'react';
import PropTypes from 'prop-types';
import { labelStyle, inputStyle, inputFocusBoxShadow } from './authStyles';
import { THEME } from '../../config';

export default function AuthField({
  label,
  type = 'text',
  value,
  onChange,
  autoComplete,
  placeholder,
  disabled = false,
  invalid = false,
  error,
  autoFocus = false,
  inputMode,
  name,
}) {
  const id = useId();
  const errorId = `${id}-error`;
  const [focused, setFocused] = useState(false);
  const showInvalid = invalid || Boolean(error);

  return (
    <div style={{ display: 'grid', gap: '6px' }}>
      <label htmlFor={id} style={labelStyle()}>
        {label}
      </label>
      <input
        id={id}
        name={name}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        placeholder={placeholder}
        disabled={disabled}
        inputMode={inputMode}
        // autoFocus is a deliberate UX choice on the first field of a screen.
        autoFocus={autoFocus}
        aria-invalid={showInvalid || undefined}
        aria-describedby={error ? errorId : undefined}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          ...inputStyle(showInvalid),
          boxShadow: focused ? inputFocusBoxShadow() : 'none',
        }}
      />
      {error ? (
        <span
          id={errorId}
          style={{ fontSize: '12px', color: THEME.colors.statusRed }}
        >
          {error}
        </span>
      ) : null}
    </div>
  );
}

AuthField.propTypes = {
  label: PropTypes.string.isRequired,
  type: PropTypes.string,
  value: PropTypes.string.isRequired,
  onChange: PropTypes.func.isRequired,
  autoComplete: PropTypes.string,
  placeholder: PropTypes.string,
  disabled: PropTypes.bool,
  invalid: PropTypes.bool,
  error: PropTypes.string,
  autoFocus: PropTypes.bool,
  inputMode: PropTypes.string,
  name: PropTypes.string,
};
