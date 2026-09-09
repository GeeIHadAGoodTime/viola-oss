import { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import ChatMessage from './ChatMessage.jsx';

const SUGGESTIONS = [
  'Plan the next stage of this project',
  'Review the current architecture',
  'Draft a focused implementation checklist',
  'Summarize what changed today',
];

export default function ChatThread({
  messages,
  streamingMessageId,
  onSuggestion,
  onRegenerate,
  onFork,
  onFeedback,
}) {
  const scrollRef = useRef(null);
  const [pinned, setPinned] = useState(true);

  useEffect(() => {
    if (!pinned || !scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, pinned]);

  const handleScroll = () => {
    const node = scrollRef.current;
    if (!node) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    setPinned(distance < 90);
  };

  if (messages.length === 0) {
    return (
      <div className="chat-thread-empty">
        <div>
          <h2>What are we working on?</h2>
          <div className="chat-suggestion-grid">
            {SUGGESTIONS.map((suggestion) => (
              <button type="button" key={suggestion} onClick={() => onSuggestion(suggestion)}>
                {suggestion}
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-thread-wrap">
      <div ref={scrollRef} className="chat-thread" onScroll={handleScroll}>
        {messages.map((message) => (
          <ChatMessage
            key={message.id}
            message={message}
            streaming={message.id === streamingMessageId}
            onRegenerate={onRegenerate}
            onFork={onFork}
            onFeedback={onFeedback}
          />
        ))}
      </div>
      {!pinned && (
        <button
          type="button"
          className="chat-scroll-bottom"
          onClick={() => {
            if (scrollRef.current) {
              scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            }
            setPinned(true);
          }}
        >
          Scroll to bottom
        </button>
      )}
    </div>
  );
}

ChatThread.propTypes = {
  messages: PropTypes.arrayOf(PropTypes.shape({
    id: PropTypes.string.isRequired,
    role: PropTypes.string.isRequired,
    content: PropTypes.string,
  })).isRequired,
  streamingMessageId: PropTypes.string,
  onSuggestion: PropTypes.func.isRequired,
  onRegenerate: PropTypes.func.isRequired,
  onFork: PropTypes.func.isRequired,
  onFeedback: PropTypes.func.isRequired,
};
