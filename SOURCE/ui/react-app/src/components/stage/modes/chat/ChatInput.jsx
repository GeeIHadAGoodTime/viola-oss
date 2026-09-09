import { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { AttachIcon, MicIcon, SendIcon, StopIcon } from '../../../icons';

export default function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  onAttachFiles,
  streaming,
  disabled,
  handlePTTStart = () => {},
  handlePTTEnd = () => {},
}) {
  const textRef = useRef(null);
  const fileInputRef = useRef(null);

  useEffect(() => {
    const node = textRef.current;
    if (!node) return;
    node.style.height = '0px';
    node.style.height = `${Math.min(node.scrollHeight, 180)}px`;
  }, [value]);

  const submit = () => {
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
      submit();
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
        disabled={disabled}
      />
      <button
        type="button"
        className="chat-icon-button"
        aria-label="Push to talk"
        title="Push to talk"
        onMouseDown={handlePTTStart}
        onMouseUp={handlePTTEnd}
        onMouseLeave={handlePTTEnd}
        onTouchStart={(event) => {
          event.preventDefault();
          handlePTTStart();
        }}
        onTouchEnd={(event) => {
          event.preventDefault();
          handlePTTEnd();
        }}
      >
        <MicIcon />
      </button>
      <button
        type="button"
        className={`chat-send-button ${streaming ? 'is-stop' : ''}`}
        onClick={submit}
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
  onAttachFiles: PropTypes.func.isRequired,
  streaming: PropTypes.bool,
  disabled: PropTypes.bool,
  handlePTTStart: PropTypes.func,
  handlePTTEnd: PropTypes.func,
};
