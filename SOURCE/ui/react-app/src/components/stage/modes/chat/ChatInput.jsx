import { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { usePressAndHold } from '../../../../hooks/usePressAndHold';
import { AttachIcon, MicIcon, SendIcon, StopIcon } from '../../../icons';

export default function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  onQueue = () => {},
  onAttachFiles,
  streaming,
  disabled,
  handlePTTStart = () => {},
  handlePTTEnd = () => {},
}) {
  const textRef = useRef(null);
  const fileInputRef = useRef(null);
  // Push-to-talk hold: pointer capture + cancel handling (see usePressAndHold).
  const pttPress = usePressAndHold({ onStart: handlePTTStart, onEnd: handlePTTEnd });

  useEffect(() => {
    const node = textRef.current;
    if (!node) return;
    node.style.height = '0px';
    node.style.height = `${Math.min(node.scrollHeight, 180)}px`;
  }, [value]);

  // Enter never stops a reply. While one is running, Enter keeps the text as
  // its own queued message (sent when the reply ends) and clears the box;
  // Stop is the button only. Enter used to press Stop and leave the draft, so
  // "pause" then "next" went out as "pausenext" (1.0.5 build 11, bug 7).
  const submitFromKeyboard = () => {
    if (!value.trim()) return;
    if (streaming) {
      onQueue(value);
      return;
    }
    if (disabled) return;
    onSend();
  };

  const pressButton = () => {
    if (streaming) {
      onStop();
      return;
    }
    if (!value.trim() || disabled) return;
    onSend();
  };

  const handleKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitFromKeyboard();
    }
  };

  return (
    <div className="chat-input-shell">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="chat-file-input"
        tabIndex={-1}
        onChange={(event) => {
          onAttachFiles(event.target.files);
          event.target.value = '';
        }}
      />
      <button
        type="button"
        className="chat-icon-button"
        disabled={disabled}
        title="Attach file"
        aria-label="Attach file"
        onClick={() => fileInputRef.current?.click()}
      >
        <AttachIcon />
      </button>
      <textarea
        ref={textRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Message Viola"
        aria-label="Message Viola"
        rows={1}
      />
      <button
        type="button"
        className="chat-icon-button"
        aria-label="Push to talk"
        title="Push to talk"
        {...pttPress}
        style={{ touchAction: 'none' }}
      >
        <MicIcon />
      </button>
      <button
        type="button"
        className={`chat-send-button ${streaming ? 'is-stop' : ''}`}
        onClick={pressButton}
        disabled={!streaming && (!value.trim() || disabled)}
        aria-label={streaming ? 'Stop response' : 'Send message'}
        title={streaming ? 'Stop response' : 'Send message'}
      >
        {streaming ? <StopIcon /> : <SendIcon />}
      </button>
    </div>
  );
}

ChatInput.propTypes = {
  value: PropTypes.string.isRequired,
  onChange: PropTypes.func.isRequired,
  onSend: PropTypes.func.isRequired,
  onStop: PropTypes.func.isRequired,
  onQueue: PropTypes.func,
  onAttachFiles: PropTypes.func.isRequired,
  streaming: PropTypes.bool,
  disabled: PropTypes.bool,
  handlePTTStart: PropTypes.func,
  handlePTTEnd: PropTypes.func,
};
