import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Modal, { secondaryButtonStyle, dangerButtonStyle } from './Modal';
import { THEME } from '../config';
import { formatTimeDisplay } from '../utils/timeFormat';
import { humanizeIdentifier } from '../utils/humanizeIdentifier';

export default function HistoryModal({ isOpen, onClose, history = [], onClearHistory, timeFormat = 'auto' }) {
  const [filter, setFilter] = useState('all');

  const filteredHistory = history.filter(item => {
    if (filter === 'all') return true;
    if (filter === 'commands') return item.type === 'user' || item.role === 'user';
    if (filter === 'responses') return item.type === 'assistant' || item.role === 'assistant';
    return true;
  });

  const formatTime = (timestamp) => {
    if (!timestamp) return '';
    return formatTimeDisplay(timestamp, timeFormat);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Chat History"
      footer={
        <>
          {history.length > 0 && (
            <button
              onClick={onClearHistory}
              aria-label="Clear all chat history"
              style={dangerButtonStyle}
              onMouseOver={(e) => e.currentTarget.style.backgroundColor = `${THEME.colors.statusRed}4D`}
              onMouseOut={(e) => e.currentTarget.style.backgroundColor = `${THEME.colors.statusRed}33`}
            >
              Clear History
            </button>
          )}
          <button
            onClick={onClose}
            aria-label="Close history modal"
            style={secondaryButtonStyle}
            onMouseOver={(e) => e.currentTarget.style.backgroundColor = THEME.colors.glassHover}
            onMouseOut={(e) => e.currentTarget.style.backgroundColor = THEME.colors.glassBase}
          >
            Close
          </button>
        </>
      }
    >
      {/* Filter buttons */}
      {history.length > 0 && (
        <div style={{
          display: 'flex',
          gap: '8px',
          marginBottom: '16px',
          paddingBottom: '16px',
          borderBottom: `1px solid ${THEME.colors.borderLight}`,
        }}>
          {['all', 'commands', 'responses'].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              aria-label={`Filter by ${f}`}
              aria-pressed={filter === f}
              style={{
                padding: '6px 14px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: filter === f ? THEME.colors.glassActive : 'transparent',
                color: filter === f ? THEME.colors.textPrimary : THEME.colors.textMuted,
                cursor: 'pointer',
                fontSize: '13px',
                fontWeight: 500,
                textTransform: 'capitalize',
              }}
            >
              {f}
            </button>
          ))}
        </div>
      )}

      {/* History list */}
      {filteredHistory.length === 0 ? (
        <div style={{ textAlign: 'center', color: THEME.colors.textMuted, padding: '40px' }}>
          {history.length === 0
            ? 'No chat history yet. Start a conversation!'
            : 'No items match the selected filter.'
          }
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {filteredHistory.map((item, index) => {
            const isUser = item.type === 'user' || item.role === 'user';
            // Create stable unique key using timestamp + content hash or index
            const uniqueKey = item.timestamp
              ? `${item.timestamp}-${(item.content || item.text || item.message || '').substring(0, 20)}`
              : `history-${index}-${(item.content || item.text || item.message || '').substring(0, 20)}`;
            // `intent` is the pipeline's own slug (`set_volume`), so this badge
            // showed things like "set_volume" to the user (#4421).
            const intentLabel = humanizeIdentifier(item.intent);
            return (
              <div
                key={uniqueKey}
                style={{
                  display: 'flex',
                  gap: '12px',
                  padding: '12px 16px',
                  backgroundColor: isUser ? THEME.colors.borderSubtle : `${THEME.colors.statusGreen}0D`,
                  borderRadius: '12px',
                  borderLeft: `3px solid ${isUser ? THEME.colors.textFaint : `${THEME.colors.statusGreen}4D`}`,
                }}
              >
                {/* Icon */}
                <div style={{
                  width: '28px',
                  height: '28px',
                  borderRadius: '8px',
                  backgroundColor: isUser ? THEME.colors.glassBase : `${THEME.colors.statusGreen}26`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                  color: isUser ? THEME.colors.textSecondary : THEME.colors.statusGreen,
                }}>
                  {isUser ? (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z" />
                    </svg>
                  ) : (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z" />
                    </svg>
                  )}
                </div>

                {/* Content */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: '4px',
                  }}>
                    <span style={{
                      fontSize: '12px',
                      color: isUser ? THEME.colors.textMuted : `${THEME.colors.statusGreen}CC`,
                      fontWeight: 500,
                    }}>
                      {isUser ? 'You' : 'Viola'}
                    </span>
                    {item.timestamp && (
                      <span style={{
                        fontSize: '11px',
                        color: THEME.colors.textDisabled,
                      }}>
                        {formatTime(item.timestamp)}
                      </span>
                    )}
                  </div>
                  <div style={{
                    color: THEME.colors.textPrimary,
                    fontSize: '14px',
                    lineHeight: 1.5,
                    wordBreak: 'break-word',
                  }}>
                    {item.content || item.text || item.message || ''}
                  </div>
                  {intentLabel && (
                    <div style={{
                      marginTop: '6px',
                      display: 'inline-block',
                      padding: '2px 8px',
                      backgroundColor: THEME.colors.borderSubtle,
                      borderRadius: '4px',
                      fontSize: '11px',
                      color: THEME.colors.textMuted,
                    }}>
                      {intentLabel}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

HistoryModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  history: PropTypes.array,
  onClearHistory: PropTypes.func,
  timeFormat: PropTypes.oneOf(['auto', '12h', '24h']),
};
