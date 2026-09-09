import React, { useState, useEffect } from 'react';
import PropTypes from 'prop-types';
import Modal, { secondaryButtonStyle, dangerButtonStyle } from './Modal';
import { useViolaApi } from '../hooks/useViolaApi';
import { THEME } from '../config';

export default function QueueModal({ isOpen, onClose, wsQueue }) {
  const [httpQueue, setHttpQueue] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState(null);
  const api = useViolaApi();

  // Use wsQueue as primary data source, HTTP fetch as fallback
  const queue = (wsQueue && wsQueue.length > 0) ? wsQueue : (httpQueue || []);

  // Fetch queue via HTTP when modal opens (fallback for when WebSocket data is not available)
  useEffect(() => {
    if (isOpen) {
      fetchQueue();
    }
  }, [isOpen]);

  const fetchQueue = async () => {
    try {
      setLoading(true);
      const result = await api.getQueue();
      if (result.ok) {
        setHttpQueue(result.queue || result.data?.queue || []);
      }
    } catch (e) {
      // Failed to fetch queue - continue with empty list
    } finally {
      setLoading(false);
    }
  };

  const handlePlayItem = async (itemId) => {
    if (actionLoading) return;
    setActionError(null);
    setActionLoading(true);
    try {
      await api.playQueueItem(itemId);
      fetchQueue();
    } catch (e) {
      setActionError('Could not play item. Please try again.');
    } finally {
      setActionLoading(false);
    }
  };

  const handleRemoveItem = async (itemId) => {
    if (actionLoading) return;
    setActionError(null);
    setActionLoading(true);
    try {
      await api.removeFromQueue(itemId);
      fetchQueue();
    } catch (e) {
      setActionError('Could not remove item. Please try again.');
    } finally {
      setActionLoading(false);
    }
  };

  const handleClearQueue = async () => {
    if (actionLoading) return;
    setActionError(null);
    setActionLoading(true);
    try {
      await api.clearQueue();
      setHttpQueue([]);
    } catch (e) {
      setActionError('Could not clear queue. Please try again.');
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Queue"
      footer={
        <>
          {queue.length > 0 && (
            <button
              onClick={handleClearQueue}
              disabled={actionLoading}
              style={{ ...dangerButtonStyle, opacity: actionLoading ? 0.6 : 1, cursor: actionLoading ? 'not-allowed' : 'pointer' }}
              onMouseOver={(e) => !actionLoading && (e.currentTarget.style.backgroundColor = `${THEME.colors.statusRed}4D`)}
              onMouseOut={(e) => !actionLoading && (e.currentTarget.style.backgroundColor = `${THEME.colors.statusRed}33`)}
            >
              {actionLoading ? 'Working...' : 'Clear Queue'}
            </button>
          )}
          <button
            onClick={onClose}
            style={secondaryButtonStyle}
            onMouseOver={(e) => e.currentTarget.style.backgroundColor = THEME.colors.glassHover}
            onMouseOut={(e) => e.currentTarget.style.backgroundColor = THEME.colors.glassBase}
          >
            Close
          </button>
        </>
      }
    >
      {actionError && (
        <div style={{ color: THEME.colors.statusRed, marginBottom: '12px', fontSize: '13px' }}>
          {actionError}
        </div>
      )}
      {loading ? (
        <div style={{ textAlign: 'center', color: THEME.colors.textMuted, padding: '40px' }}>
          Loading queue...
        </div>
      ) : queue.length === 0 ? (
        <div style={{ textAlign: 'center', color: THEME.colors.textMuted, padding: '40px' }}>
          Queue is empty. Ask me to play some music!
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {queue.map((item, index) => (
            <div
              key={item.id || `queue-item-${index}`}
              role="listitem"
              aria-label={`Queue position ${index + 1}: ${item.title || 'Unknown Track'}${item.artist ? ` by ${item.artist}` : ''}`}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                padding: '12px 16px',
                backgroundColor: index === 0 ? `${THEME.colors.statusGreen}1A` : THEME.colors.borderSubtle,
                borderRadius: '12px',
                border: index === 0 ? `1px solid ${THEME.colors.statusGreen}33` : '1px solid transparent',
              }}
            >
              {/* Index/Now Playing indicator */}
              <div style={{
                width: '28px',
                height: '28px',
                borderRadius: '8px',
                backgroundColor: index === 0 ? `${THEME.colors.statusGreen}33` : THEME.colors.glassBase,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '12px',
                fontWeight: 600,
                color: index === 0 ? THEME.colors.statusGreen : THEME.colors.textMuted,
                flexShrink: 0,
              }}>
                {index === 0 ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M8 5v14l11-7z" />
                  </svg>
                ) : (
                  index + 1
                )}
              </div>

              {/* Thumbnail */}
              {item.thumbnail_url && (
                <img
                  src={item.thumbnail_url}
                  alt=""
                  style={{
                    width: '48px',
                    height: '48px',
                    borderRadius: '8px',
                    objectFit: 'cover',
                    flexShrink: 0,
                  }}
                />
              )}

              {/* Track info */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  color: THEME.colors.textPrimary,
                  fontSize: '14px',
                  fontWeight: 500,
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}>
                  {item.title || 'Unknown Track'}
                </div>
                {item.artist && (
                  <div style={{
                    color: THEME.colors.textMuted,
                    fontSize: '12px',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}>
                    {item.artist}
                  </div>
                )}
              </div>

              {/* Actions */}
              <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                {index > 0 && (
                  <button
                    onClick={() => handlePlayItem(item.id)}
                    disabled={actionLoading}
                    aria-label={`Play ${item.title || 'track'} now`}
                    style={{
                      background: 'none',
                      border: 'none',
                      color: THEME.colors.textMuted,
                      cursor: actionLoading ? 'not-allowed' : 'pointer',
                      padding: '6px',
                      borderRadius: '6px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      opacity: actionLoading ? 0.4 : 1,
                    }}
                    onMouseOver={(e) => !actionLoading && (e.currentTarget.style.color = THEME.colors.textPrimary)}
                    onMouseOut={(e) => e.currentTarget.style.color = THEME.colors.textMuted}
                    title="Play now"
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M8 5v14l11-7z" />
                    </svg>
                  </button>
                )}
                <button
                  onClick={() => handleRemoveItem(item.id)}
                  disabled={actionLoading}
                  aria-label={`Remove ${item.title || 'track'} from queue`}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: THEME.colors.textMuted,
                    cursor: actionLoading ? 'not-allowed' : 'pointer',
                    padding: '6px',
                    borderRadius: '6px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    opacity: actionLoading ? 0.4 : 1,
                  }}
                  onMouseOver={(e) => !actionLoading && (e.currentTarget.style.color = THEME.colors.statusRed)}
                  onMouseOut={(e) => e.currentTarget.style.color = THEME.colors.textMuted}
                  title="Remove from queue"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

QueueModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  wsQueue: PropTypes.array,
};
