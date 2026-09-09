export function createCoreStageCommands({
  openMode,
  openSettings,
}) {
  return [
    {
      id: 'mode-music',
      label: 'Open Music',
      group: 'Modes',
      keywords: ['player', 'now playing'],
      perform: () => openMode('music'),
    },
    {
      id: 'mode-phone',
      label: 'Open Phone',
      group: 'Modes',
      keywords: ['calls', 'history'],
      perform: () => openMode('phone'),
    },
    {
      id: 'settings',
      label: 'Settings',
      group: 'App',
      keywords: ['preferences', 'account'],
      perform: openSettings,
    },
  ];
}

export function createChatStageCommands({
  openMode,
  dispatchNewChat,
}) {
  return [
    {
      id: 'mode-chat',
      label: 'Open Chat',
      group: 'Chat',
      keywords: ['conversation', 'thread', 'deeper work'],
      perform: () => openMode('chat'),
    },
    {
      id: 'new-chat',
      label: 'New Chat',
      group: 'Chat',
      keywords: ['thread', 'conversation'],
      perform: () => {
        dispatchNewChat();
        openMode('chat');
      },
    },
  ];
}

export function createBrowserStageCommands({
  openMode,
}) {
  return [
    {
      id: 'mode-agent-browser',
      label: 'Open Agent Browser',
      group: 'Browser',
      keywords: ['browser', 'agent', 'web'],
      perform: () => openMode('agent'),
    },
  ];
}
