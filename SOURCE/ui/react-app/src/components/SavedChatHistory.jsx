import { useEffect, useState } from 'react';
import { apiFetch } from '../hooks/useViolaApi';
import { THEME } from '../config';
import { secondaryButtonStyle } from './Modal';

const consentMessage = 'To view saved chats, enable Cloud Sync in Settings → Account → Privacy & Data.';

function readHistoryItems(data, key, validItem) {
  if (data?.ok === false) {
    throw Object.assign(new Error('Saved history request failed'), { code: data.error?.code });
  }
  const items = data?.[key];
  if (!Array.isArray(items) || !items.every(validItem)) {
    throw new Error('Saved history response has an invalid shape');
  }
  return items;
}

const validThread = thread => thread && typeof thread.id === 'string' && thread.id.length > 0
  && (thread.title == null || typeof thread.title === 'string');
const validMessage = message => message && typeof message.role === 'string'
  && (message.id == null || typeof message.id === 'string')
  && (!['user', 'assistant'].includes(message.role) || typeof message.content === 'string');

// Read the same principal-scoped store as ChatMode. The parent keys this
// component by account identity, so an account switch drops both cached data
// and outstanding requests before the new account's history can render.
export default function SavedChatHistory() {
  const [threads, setThreads] = useState([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [listAttempt, setListAttempt] = useState(0);
  const [selected, setSelected] = useState(null);
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setListLoading(true);
    setListError('');
    apiFetch('/v1/chat/threads').then((data) => {
      if (!cancelled) setThreads(readHistoryItems(data, 'threads', validThread));
    }).catch((err) => {
      if (!cancelled) setListError(err?.code === 'consent_required'
        ? consentMessage : 'Could not load saved chats. Try again.');
    }).finally(() => {
      if (!cancelled) setListLoading(false);
    });
    return () => { cancelled = true; };
  }, [listAttempt]);

  useEffect(() => {
    if (!selected) return undefined;
    let cancelled = false;
    setLoading(true);
    setError('');
    setMessages([]);
    apiFetch(`/v1/chat/threads/${encodeURIComponent(selected.id)}`).then((data) => {
      if (!cancelled) setMessages(readHistoryItems(data, 'messages', validMessage).filter((message) => (
        message.role === 'user' || message.role === 'assistant'
      )));
    }).catch((err) => {
      if (!cancelled) setError(err?.code === 'consent_required'
        ? consentMessage : 'Could not load this conversation. Try again.');
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [selected, attempt]);

  return (
    <section aria-label="Saved chats" style={{ marginBottom: 24 }}>
      <h3 style={{ color: THEME.colors.textPrimary, fontSize: 16 }}>Saved chats</h3>
      {listLoading ? <p role="status">Loading saved chats...</p> : listError ? (
        <div>
          <p role="alert">{listError}</p>
          <button type="button" style={secondaryButtonStyle} onClick={() => setListAttempt(n => n + 1)}>
            Retry saved chats
          </button>
        </div>
      ) : threads.length === 0 ? <p>No saved chats yet.</p> : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {threads.map((thread) => (
            <button key={thread.id} type="button" style={secondaryButtonStyle}
              aria-pressed={selected?.id === thread.id} onClick={() => {
                if (selected?.id === thread.id) return;
                setLoading(true);
                setSelected(thread);
              }}>
              {thread.title || 'New chat'}
            </button>
          ))}
        </div>
      )}
      {selected && (
        <section aria-label={`Conversation: ${selected.title || 'New chat'}`} style={{ marginTop: 16 }}>
          <h4>{selected.title || 'New chat'}</h4>
          {loading ? <p role="status">Loading conversation...</p> : error ? (
            <div>
              <p role="alert">{error}</p>
              <button type="button" style={secondaryButtonStyle} onClick={() => setAttempt(n => n + 1)}>
                Retry conversation
              </button>
            </div>
          ) : messages.length === 0 ? <p>No messages in this conversation yet.</p> : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {messages.map((message, index) => (
                <div key={message.id || index} style={{
                  padding: '12px 16px', borderRadius: 12,
                  backgroundColor: THEME.colors.borderSubtle,
                }}>
                  <div style={{ color: THEME.colors.textMuted, fontSize: 12, marginBottom: 4 }}>
                    {message.role === 'user' ? 'You' : 'Viola'}
                  </div>
                  <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{message.content || ''}</div>
                </div>
              ))}
            </div>
          )}
        </section>
      )}
    </section>
  );
}
