import { useState } from 'react';
import PropTypes from 'prop-types';
import Markdown from './markdown.jsx';
import ToolUseCard from './ToolUseCard.jsx';

function copyText(text) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text || '').catch(() => {});
}

export default function ChatMessage({
  message,
  streaming,
  onRegenerate,
  onFork,
  onFeedback,
}) {
  const [copied, setCopied] = useState(false);
  const role = message.role === 'assistant' ? 'assistant' : 'user';
  const rating = message.metadata?.rating || null;
  const metadataTools = Array.isArray(message.metadata?.tools) ? message.metadata.tools : [];
  const tools = Array.isArray(message.tools) ? message.tools : metadataTools;
  const serverBacked = Boolean(message.id)
    && !message.id.startsWith('streaming-')
    && !message.id.startsWith('local-user-')
    && !message.metadata?.optimistic;
  const serverActionsEnabled = serverBacked && !streaming;

  const handleCopy = () => {
    copyText(message.content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1100);
  };

  return (
    <article className={`chat-message chat-message-${role}`} data-message-id={message.id}>
      <div className="chat-avatar" aria-hidden="true">{role === 'assistant' ? 'V' : 'U'}</div>
      <div className="chat-message-body">
        <div className="chat-message-meta">
          <span>{role === 'assistant' ? 'Viola' : 'You'}</span>
          {message.status === 'stopped' && <span>Stopped</span>}
          {message.status === 'error' && <span>Error</span>}
        </div>
        {tools.length > 0 && (
          <div className="chat-tool-list">
            {tools.map((tool, index) => (
              <ToolUseCard
                key={`${tool.stream_id || 'stream'}-${tool.tool_name || 'tool'}-${tool.step_number || index}`}
                tool={tool}
              />
            ))}
          </div>
        )}
        <Markdown content={message.content || (streaming ? ' ' : '')} />
        {streaming && <span className="chat-cursor" aria-hidden="true" />}
        <div className="chat-message-actions">
          <button type="button" onClick={handleCopy}>{copied ? 'Copied' : 'Copy'}</button>
          {role === 'assistant' && (
            <button type="button" onClick={() => onRegenerate(message)} disabled={!serverActionsEnabled}>
              Regenerate
            </button>
          )}
          <button type="button" onClick={() => onFork(message)} disabled={!serverActionsEnabled}>Edit</button>
          {role === 'assistant' && (
            <>
              <button
                type="button"
                className={rating === 'up' ? 'is-selected' : ''}
                disabled={!serverActionsEnabled}
                onClick={() => onFeedback(message, rating === 'up' ? null : 'up')}
                aria-label="Thumbs up"
              >
                Up
              </button>
              <button
                type="button"
                className={rating === 'down' ? 'is-selected' : ''}
                disabled={!serverActionsEnabled}
                onClick={() => onFeedback(message, rating === 'down' ? null : 'down')}
                aria-label="Thumbs down"
              >
                Down
              </button>
            </>
          )}
        </div>
      </div>
    </article>
  );
}

ChatMessage.propTypes = {
  message: PropTypes.shape({
    id: PropTypes.string.isRequired,
    role: PropTypes.string.isRequired,
    content: PropTypes.string,
    status: PropTypes.string,
    metadata: PropTypes.shape({
      optimistic: PropTypes.bool,
      rating: PropTypes.oneOf(['up', 'down']),
      streaming: PropTypes.shape({
        mode: PropTypes.string,
        token_count: PropTypes.number,
        native: PropTypes.bool,
      }),
      tools: PropTypes.arrayOf(PropTypes.object),
    }),
    tools: PropTypes.arrayOf(PropTypes.object),
  }).isRequired,
  streaming: PropTypes.bool,
  onRegenerate: PropTypes.func.isRequired,
  onFork: PropTypes.func.isRequired,
  onFeedback: PropTypes.func.isRequired,
};
