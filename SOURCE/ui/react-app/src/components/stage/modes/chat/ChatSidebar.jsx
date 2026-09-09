import { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { ChevronLeftIcon, ChevronRightIcon } from '../../../icons';

const DAY_MS = 24 * 60 * 60 * 1000;

function groupThreads(threads) {
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const groups = {
    Today: [],
    Yesterday: [],
    'Last 7 days': [],
    'Last 30 days': [],
    Older: [],
  };
  threads.forEach((thread) => {
    const updated = Number(thread.updated_at || thread.created_at || 0) * 1000;
    if (updated >= todayStart) groups.Today.push(thread);
    else if (updated >= todayStart - DAY_MS) groups.Yesterday.push(thread);
    else if (updated >= todayStart - 7 * DAY_MS) groups['Last 7 days'].push(thread);
    else if (updated >= todayStart - 30 * DAY_MS) groups['Last 30 days'].push(thread);
    else groups.Older.push(thread);
  });
  return groups;
}

export default function ChatSidebar({
  threads,
  activeThreadId,
  search,
  collapsed,
  onSearch,
  onToggleCollapsed,
  onNewChat,
  onSelectThread,
  onRename,
  onDelete,
  onExport,
  onOpenSettings = () => {},
  profileName,
}) {
  const [menuThreadId, setMenuThreadId] = useState(null);
  const grouped = useMemo(() => groupThreads(threads), [threads]);

  if (collapsed) {
    return (
      <aside className="chat-sidebar is-collapsed">
        <button type="button" className="chat-collapse-edge" onClick={onToggleCollapsed} aria-label="Expand chat sidebar" title="Expand chat sidebar">
          <ChevronRightIcon />
        </button>
        <button type="button" className="chat-new-mini" onClick={onNewChat} aria-label="New chat">+</button>
      </aside>
    );
  }

  return (
    <aside className="chat-sidebar">
      <div className="chat-sidebar-top">
        <button type="button" className="chat-new-button" onClick={onNewChat}>New chat</button>
        <button type="button" className="chat-collapse-button" onClick={onToggleCollapsed} aria-label="Collapse chat sidebar" title="Collapse chat sidebar">
          <ChevronLeftIcon />
        </button>
      </div>
      <input
        className="chat-search"
        value={search}
        onChange={(event) => onSearch(event.target.value)}
        placeholder="Search chats"
        aria-label="Search chats"
      />
      <div className="chat-thread-list">
        {Object.entries(grouped).map(([label, items]) => (
          items.length > 0 && (
            <section key={label} className="chat-thread-group">
              <h3>{label}</h3>
              {items.map((thread) => (
                <div
                  key={thread.id}
                  className={`chat-thread-row ${activeThreadId === thread.id ? 'is-active' : ''}`}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    setMenuThreadId(thread.id);
                  }}
                >
                  <button type="button" className="chat-thread-select" onClick={() => onSelectThread(thread.id)}>
                    <span>{thread.title || 'New chat'}</span>
                  </button>
                  <button
                    type="button"
                    className="chat-thread-menu-button"
                    aria-label="Conversation menu"
                    onClick={() => setMenuThreadId(menuThreadId === thread.id ? null : thread.id)}
                  >
                    ...
                  </button>
                  {menuThreadId === thread.id && (
                    <div className="chat-thread-menu">
                      <button type="button" onClick={() => { setMenuThreadId(null); onRename(thread); }}>Rename</button>
                      <button type="button" onClick={() => { setMenuThreadId(null); onExport(thread); }}>Export</button>
                      <button type="button" className="danger" onClick={() => { setMenuThreadId(null); onDelete(thread); }}>Delete</button>
                    </div>
                  )}
                </div>
              ))}
            </section>
          )
        ))}
      </div>
      <div className="chat-profile-sliver">
        <div className="chat-profile-avatar">{(profileName || 'U').slice(0, 1).toUpperCase()}</div>
        <div>
          <span>{profileName || 'Local user'}</span>
          <small>Chat workspace</small>
        </div>
        <button type="button" onClick={onOpenSettings} aria-label="Open settings">Settings</button>
      </div>
    </aside>
  );
}

ChatSidebar.propTypes = {
  threads: PropTypes.array.isRequired,
  activeThreadId: PropTypes.string,
  search: PropTypes.string.isRequired,
  collapsed: PropTypes.bool.isRequired,
  onSearch: PropTypes.func.isRequired,
  onToggleCollapsed: PropTypes.func.isRequired,
  onNewChat: PropTypes.func.isRequired,
  onSelectThread: PropTypes.func.isRequired,
  onRename: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
  onExport: PropTypes.func.isRequired,
  onOpenSettings: PropTypes.func,
  profileName: PropTypes.string,
};
