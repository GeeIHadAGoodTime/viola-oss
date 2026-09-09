export const DEFAULT_PTT_HOTKEY = 'Space';
// Mute (mic mute / pause wake word) hotkey. Deliberately different from
// DEFAULT_PTT_HOTKEY so the two device hotkeys never collide out of the
// box -- the backend also rejects a save where they'd match (see
// _validate_hotkey_cross_field_requirements in ui/settings_api.py).
export const DEFAULT_MUTE_HOTKEY = 'Ctrl+M';

const MODIFIER_ALIASES = {
  ctrl: 'ctrl',
  control: 'ctrl',
  alt: 'alt',
  option: 'alt',
  shift: 'shift',
  meta: 'meta',
  cmd: 'meta',
  command: 'meta',
  win: 'meta',
  windows: 'meta',
  super: 'meta',
};

const CODE_ALIASES = {
  ' ': 'Space',
  space: 'Space',
  spacebar: 'Space',
  esc: 'Escape',
  escape: 'Escape',
  enter: 'Enter',
  return: 'Enter',
  tab: 'Tab',
  backspace: 'Backspace',
  delete: 'Delete',
  del: 'Delete',
  up: 'ArrowUp',
  down: 'ArrowDown',
  left: 'ArrowLeft',
  right: 'ArrowRight',
};

function normalizeCodeToken(rawToken) {
  const token = String(rawToken || '').trim();
  if (!token) return '';

  const lower = token.toLowerCase();
  if (CODE_ALIASES[lower]) return CODE_ALIASES[lower];
  if (/^key[a-z]$/i.test(token)) return `Key${token.slice(-1).toUpperCase()}`;
  if (/^digit[0-9]$/i.test(token)) return `Digit${token.slice(-1)}`;
  if (/^numpad[0-9]$/i.test(token)) return `Numpad${token.slice(-1)}`;
  if (/^f([1-9]|1[0-9]|2[0-4])$/i.test(token)) return token.toUpperCase();
  if (/^[a-z]$/i.test(token)) return `Key${token.toUpperCase()}`;
  if (/^[0-9]$/.test(token)) return `Digit${token}`;
  return token.charAt(0).toUpperCase() + token.slice(1);
}

export function parseHotkey(hotkey = DEFAULT_PTT_HOTKEY) {
  const parts = String(hotkey || DEFAULT_PTT_HOTKEY)
    .split('+')
    .map((part) => part.trim())
    .filter(Boolean);
  const tokens = parts.length ? parts : [DEFAULT_PTT_HOTKEY];
  const modifiers = {
    ctrl: false,
    alt: false,
    shift: false,
    meta: false,
  };
  let code = '';

  tokens.forEach((token) => {
    const modifier = MODIFIER_ALIASES[token.toLowerCase()];
    if (modifier) {
      modifiers[modifier] = true;
      return;
    }
    code = normalizeCodeToken(token);
  });

  return {
    ...modifiers,
    code: code || normalizeCodeToken(DEFAULT_PTT_HOTKEY),
  };
}

export function isHotkeyEvent(event, hotkey = DEFAULT_PTT_HOTKEY) {
  const parsed = parseHotkey(hotkey);
  return Boolean(
    event
      && event.code === parsed.code
      && Boolean(event.ctrlKey) === parsed.ctrl
      && Boolean(event.altKey) === parsed.alt
      && Boolean(event.shiftKey) === parsed.shift
      && Boolean(event.metaKey) === parsed.meta
  );
}

export function hotkeyFromKeyboardEvent(event) {
  if (!event) return DEFAULT_PTT_HOTKEY;
  const modifiers = [];
  if (event.ctrlKey) modifiers.push('Ctrl');
  if (event.altKey) modifiers.push('Alt');
  if (event.shiftKey) modifiers.push('Shift');
  if (event.metaKey) modifiers.push('Meta');
  const code = event.code && !['ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'].includes(event.code)
    ? event.code
    : '';
  if (!code && modifiers.length === 0) return DEFAULT_PTT_HOTKEY;
  return [...modifiers, code].filter(Boolean).join('+') || DEFAULT_PTT_HOTKEY;
}

export function isEditableShortcutTarget(target) {
  if (!target || typeof target !== 'object') return false;
  if (target.isContentEditable) return true;
  const tagName = String(target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tagName)) return true;
  if (typeof target.closest === 'function') {
    return Boolean(target.closest('[contenteditable="true"],[role="textbox"],[data-hotkeys-ignore="true"]'));
  }
  return false;
}

export function isCommandPaletteShortcut(event) {
  return Boolean(
    event
      && (event.ctrlKey || event.metaKey)
      && !event.altKey
      && String(event.key || '').toLowerCase() === 'k'
  );
}

export function shouldIgnoreCommandPaletteShortcut(event, uiState = {}) {
  if (uiState.modalOpen || uiState.commandPaletteOpen) return true;
  return isEditableShortcutTarget(event?.target);
}
