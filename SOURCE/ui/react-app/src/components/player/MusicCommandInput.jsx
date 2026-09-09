/**
 * MusicCommandInput — typed request box for the music tab.
 *
 * Desktop lets the user type a music request via global type-anywhere capture
 * (SmartDisplay's document keydown handler); the browser/mobile music tab
 * previously offered only voice + transport controls, with no equivalent for
 * touch users (iOS needs a focusable input). This gives typed parity there:
 * the text submits through the SAME path a typed chat/command uses (POST
 * /v1/command via api.sendCommand -> handleCommandResult in SmartDisplay),
 * reaching Viola's agent exactly like the onboarding "try a command" box.
 * This matters because voice can be billing-blocked, leaving typed input the
 * only way in.
 *
 * This component itself is surface-agnostic; PlayerSection.jsx is the one
 * that decides WHEN to render it — hidden on the desktop Qt shell (#1504),
 * where this box would duplicate type-anywhere and clutter the hero surface.
 */
import { useState } from 'react';
import PropTypes from 'prop-types';
import { SendIcon } from '../icons';
import styles from './MusicCommandInput.module.css';

const MusicCommandInput = ({ onSubmit, disabled }) => {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const clean = text.trim();
    if (!clean || busy || disabled) return;
    setBusy(true);
    try {
      await onSubmit(clean);
      setText('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className={`music-command-input ${styles.form}`}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <input
        type="text"
        className={styles.input}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Ask Viola to play something"
        aria-label="Ask Viola to play something"
        disabled={disabled}
        enterKeyHint="send"
      />
      <button
        type="submit"
        className={styles.send}
        disabled={busy || disabled || !text.trim()}
        aria-label="Send music request"
        title="Send music request"
      >
        <SendIcon />
      </button>
    </form>
  );
};

MusicCommandInput.propTypes = {
  onSubmit: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
};

export default MusicCommandInput;
