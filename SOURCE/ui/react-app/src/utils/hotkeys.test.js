import { describe, expect, it } from 'vitest';
import {
  DEFAULT_MUTE_HOTKEY,
  DEFAULT_PTT_HOTKEY,
  hotkeyFromKeyboardEvent,
  isCommandPaletteShortcut,
  isHotkeyEvent,
  parseHotkey,
  shouldIgnoreCommandPaletteShortcut,
} from './hotkeys';

function event(overrides = {}) {
  return {
    code: 'Space',
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    metaKey: false,
    ...overrides,
  };
}

describe('hotkey helpers', () => {
  it('defaults to Space and rejects extra modifiers', () => {
    expect(parseHotkey('').code).toBe('Space');
    expect(isHotkeyEvent(event(), '')).toBe(true);
    expect(isHotkeyEvent(event({ ctrlKey: true }), '')).toBe(false);
  });

  it('matches modifier combos with KeyboardEvent.code values', () => {
    expect(isHotkeyEvent(event({ code: 'KeyP', ctrlKey: true, shiftKey: true }), 'Ctrl+Shift+KeyP')).toBe(true);
    expect(isHotkeyEvent(event({ code: 'KeyP', ctrlKey: true }), 'Ctrl+Shift+KeyP')).toBe(false);
    expect(isHotkeyEvent(event({ code: 'Digit1', altKey: true }), 'Alt+1')).toBe(true);
  });

  it('captures hotkeys from keyboard events using code names', () => {
    expect(hotkeyFromKeyboardEvent(event({ code: 'KeyV', ctrlKey: true }))).toBe('Ctrl+KeyV');
    expect(hotkeyFromKeyboardEvent(event({ code: 'Space' }))).toBe('Space');
  });

  it('defaults the mute hotkey to Ctrl+M, distinct from the PTT default', () => {
    expect(DEFAULT_MUTE_HOTKEY).toBe('Ctrl+M');
    expect(DEFAULT_MUTE_HOTKEY).not.toBe(DEFAULT_PTT_HOTKEY);
    expect(isHotkeyEvent(event({ code: 'KeyM', ctrlKey: true }), DEFAULT_MUTE_HOTKEY)).toBe(true);
    // The default mute combo must not accidentally match the default PTT combo.
    expect(isHotkeyEvent(event({ code: 'KeyM', ctrlKey: true }), DEFAULT_PTT_HOTKEY)).toBe(false);
    expect(isHotkeyEvent(event({ code: 'Space' }), DEFAULT_MUTE_HOTKEY)).toBe(false);
  });

  it('guards global command palette shortcuts in editable and modal contexts', () => {
    const input = document.createElement('input');
    const div = document.createElement('div');
    const commandEvent = event({ key: 'k', code: 'KeyK', ctrlKey: true, target: div });

    expect(isCommandPaletteShortcut(commandEvent)).toBe(true);
    expect(shouldIgnoreCommandPaletteShortcut(commandEvent)).toBe(false);
    expect(shouldIgnoreCommandPaletteShortcut({ ...commandEvent, target: input })).toBe(true);
    expect(shouldIgnoreCommandPaletteShortcut(commandEvent, { modalOpen: true })).toBe(true);
    expect(shouldIgnoreCommandPaletteShortcut(commandEvent, { commandPaletteOpen: true })).toBe(true);
  });
});
