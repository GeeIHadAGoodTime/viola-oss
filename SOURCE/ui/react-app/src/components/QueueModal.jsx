import React, { useState, useEffect, useRef, useCallback, useLayoutEffect } from 'react';
import PropTypes from 'prop-types';
import Modal, { secondaryButtonStyle, dangerButtonStyle } from './Modal';
import { useViolaApi } from '../hooks/useViolaApi';
import { THEME } from '../config';

export default function QueueModal({ isOpen, onClose, wsQueue }) {
  const [httpQueue, setHttpQueue] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionState, setActionState] = useState({ generation: 0, pending: false, error: null });
  const sessionRef = useRef({ generation: 0, open: isOpen, active: true, pending: null, read: 0 });
  const session = sessionRef.current;
  const retireSession = useCallback(() => {
    if (session.pending) clearTimeout(session.pending.timer);
    session.pending = null;
    session.generation += 1;
    session.read += 1;
  }, [session]);
  const actionLoading = actionState.generation === session.generation && actionState.pending;
  const actionError = actionState.generation === session.generation ? actionState.error : null;
  const api = useViolaApi();

  // Use wsQueue as primary data source, HTTP fetch as fallback.
  const hasPlayerQueue = Array.isArray(wsQueue);
  const queue = hasPlayerQueue ? wsQueue : (httpQueue || []);

  useLayoutEffect(() => {
    session.active = true;
    return () => {
      session.active = false;
      retireSession();
    };
  }, [session, retireSession]);

  useLayoutEffect(() => {
    if (session.open !== isOpen) {
      retireSession();
      session.open = isOpen;
      setActionState({ generation: session.generation, pending: false, error: null });
    }
  }, [isOpen, session, retireSession]);

  const fetchQueue = useCallback(async () => {
    const generation = session.generation;
    const read = ++session.read;
    const ownsRead = () => session.active && session.open && session.generation === generation && session.read === read;
    try {
      setLoading(true);
      const result = await api.getQueue();
      if (ownsRead() && result?.ok === true) {
        const received = result.queue ?? result.data?.queue;
        if (Array.isArray(received)) setHttpQueue(received);
      }
    } catch {
      // Generic fallback read/retry feedback remains a separate UI contract.
    } finally {
      if (ownsRead()) setLoading(false);
    }
  }, [api, session]);

  useEffect(() => {
    if (isOpen) fetchQueue();
  }, [isOpen, fetchQueue]);

  const closeModal = () => {
    retireSession();
    setActionState({ generation: session.generation, pending: false, error: null });
    onClose();
  };

  const runAction = async (request, accepted, failure) => {
    if (!session.active || !session.open || session.pending) return;
    const owner = { generation: session.generation, deadline: performance.now() + 15000, timer: null };
    const ownsAction = () => session.active && session.open && session.generation === owner.generation && session.pending === owner;
    const settle = (error) => {
      if (!ownsAction()) return;
      clearTimeout(owner.timer);
      session.pending = null;
      setActionState({ generation: owner.generation, pending: false, error });
    };
    const uncertainty = 'The queue request is taking too long. Its result is unknown. Please check the queue before trying again.';
    session.pending = owner;
    setActionState({ generation: owner.generation, pending: true, error: null });
    owner.timer = setTimeout(() => settle(uncertainty), 15000);
    try {
      const result = await request();
      if (!ownsAction()) return;
      if (performance.now() >= owner.deadline) {
        settle(uncertainty);
        return;
      }
      if (result?.ok !== true) {
        settle(failure);
        return;
      }
      accepted();
      settle(null);
    } catch {
      if (ownsAction()) settle(performance.now() >= owner.deadline ? uncertainty : failure);
    }
  };

  const handlePlayItem = (itemId) => runAction(
    () => api.playQueueItem(itemId), fetchQueue, 'Could not play item. Please try again.',
  );
  const handleRemoveItem = (itemId) => runAction(
    () => api.removeFromQueue(itemId), fetchQueue, 'Could not remove item. Please try again.',
  );
  const handleClearQueue = () => runAction(
    () => api.clearQueue(),
    () => { session.read += 1; setHttpQueue([]); setLoading(false); },
    'Could not clear queue. Please try again.',
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={closeModal}
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
            onClick={closeModal}
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
        <div role="alert" style={{ color: THEME.colors.statusRed, marginBottom: '12px', fontSize: '13px' }}>
          {actionError}
        </div>
      )}
      {loading && !hasPlayerQueue ? (
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
                backgroundColor: THEME.colors.borderSubtle,
                borderRadius: '12px',
                border: '1px solid transparent',
              }}
            >
              {/* The queue contains upcoming tracks; now_playing is a separate state field. */}
              <div style={{
                width: '28px',
                height: '28px',
                borderRadius: '8px',
                backgroundColor: THEME.colors.glassBase,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '12px',
                fontWeight: 600,
                color: THEME.colors.textMuted,
                flexShrink: 0,
              }}>
                {index + 1}
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
                {
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
                }
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
