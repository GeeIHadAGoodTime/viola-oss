// Settings search: which tabs match what the user typed in the search box.
//
// Each tab is found by its own name, a few keywords, and the names of the
// settings it shows. The setting names used to be missing: searching "crash"
// said "No settings match" although Account > Privacy & Data has "Automatic
// Crash Reports" (1.0.5 build 7, A2.8). SettingsModal.search.test.jsx reads
// the setting titles out of SettingsModal.jsx and CustomizeTab.jsx and fails
// when one is not findable here, so a new setting cannot be missed again.

const SECTION_KEYWORDS = {
  account: [
    'account profile plan billing payment methods cards usage upgrade sync devices sign out',
    'phone number recording ai disclosure retention delete call data',
    'privacy data telemetry usage reports sync error reporting session replay wake-word training',
  ],
  ai_agents: [
    'ai source managed byok chatgpt plus codex local model autonomy solo ensemble symphony browser weekly review',
    'agent source reasoning provider browser import session',
  ],
  music: [
    'music source youtube spotify local files sign in playlists playback volume autoplay',
  ],
  voice: [
    'voice wake word sensitivity push-to-talk ptt shortcut text to speech tts voice speed volume stt recognition whisper vad advanced',
  ],
  connections: [
    'connections connected services smart home calendar oauth google telegram qr messaging',
  ],
  customize: [
    'customize theme accent color weather location city zip postal code time format wake word selection',
  ],
  system: [
    'system audio devices microphone speaker multi-room rooms notifications updates early access window tray startup network api port advanced settings',
  ],
};

// The settings each tab shows, by the title the user reads on screen.
export const SETTING_TITLES_BY_TAB = {
  account: [
    'Billing & Payment Methods', 'Payment PIN', 'Privacy & Data', 'Usage Reports', 'Cloud Sync',
    'Cloud Speech-to-Text', 'Error Reporting', 'Automatic Crash Reports', 'Session Replay',
    'Wake-Word Training Contribution', 'About', 'Version', 'Service', 'Last Updated',
    'Record Phone Calls', 'Keep Phone Transcript', 'Announce AI On Calls', 'Text messages',
  ],
  ai_agents: [
    'Weekly Review', 'AI Source', 'Provider Connection', 'Provider / preset', 'Agent Mode', 'Agent Autonomy',
  ],
  music: [
    'Music Source', 'Local music folder', 'Playback', 'Default Volume', 'Autoplay Enabled',
    'AI-Powered Autoplay', 'Playlists',
  ],
  voice: [
    'Hands-Free (This Browser)', 'Wake Sensitivity', 'Push-to-Talk', 'Voice Input Mode',
    'Push-to-Talk Shortcut', 'Mute Microphone', 'Mute Shortcut', 'Text to Speech', 'Speak Responses Aloud',
    'Speak Typed Replies Too', 'Speech Output Language', 'Assistant Voice', 'Speaking Speed',
    'Assistant Volume', 'Voice Recognition & Advanced', 'Double-check the wake word', 'Voice Recognition',
    'Accuracy Level', 'Processing Hardware', 'Voice Language',
  ],
  connections: ['Calendar', 'Telegram', 'Smart Home', 'iCloud Calendar'],
  customize: ['Look', 'Color Theme', 'Weather', 'Personality', 'Voice & Wake Word', 'Reset'],
  system: [
    'Let devices on your Wi-Fi connect', 'Desktop settings', 'Audio Devices', 'Microphone', 'Speaker',
    'Notifications', 'Desktop Notifications', 'Window', 'Keep Viola running in the tray when the window is closed',
    'Start on Windows Boot', 'Start at Login', 'Start at Startup', 'Quit Viola', 'Updates', 'Check for New Versions', 'Advanced',
    'Network API Port',
  ],
};

function haystackFor(tab) {
  return [
    tab.label,
    ...(SECTION_KEYWORDS[tab.id] || []),
    ...(SETTING_TITLES_BY_TAB[tab.id] || []),
  ].join(' ').toLowerCase();
}

// A tab matches when the whole query appears in it, or every word of the
// query does ("crash reports", "reports crash" and "crash" all find Account).
export function matchesSearch(tab, query) {
  const normalizedQuery = String(query || '').trim().toLowerCase();
  if (!normalizedQuery) return true;
  const haystack = haystackFor(tab);
  if (haystack.includes(normalizedQuery)) return true;
  const words = normalizedQuery.split(/\s+/).filter(Boolean);
  return words.length > 1 && words.every((word) => haystack.includes(word));
}
