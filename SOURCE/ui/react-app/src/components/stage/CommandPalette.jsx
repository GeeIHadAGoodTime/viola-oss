import { useEffect, useId, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import styles from './CommandPalette.module.css';

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function commandMatches(command, query) {
  if (!query) return true;
  const haystack = [
    command.label,
    command.group,
    ...(command.keywords || []),
  ].filter(Boolean).join(' ').toLowerCase();
  const needle = query.toLowerCase().trim();
  if (!needle) return true;
  return needle.split(/\s+/).every((part) => haystack.includes(part));
}

export default function CommandPalette({
  open,
  commands,
  onClose,
}) {
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef(null);
  const paletteRef = useRef(null);
  const previousFocusRef = useRef(null);
  const listboxId = useId();

  const filteredCommands = useMemo(
    () => commands.filter((command) => commandMatches(command, query)),
    [commands, query]
  );
  const activeCommand = filteredCommands[activeIndex];
  const activeOptionId = activeCommand ? `${listboxId}-option-${activeCommand.id}` : undefined;

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setQuery('');
    setActiveIndex(0);
    window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      previousFocusRef.current?.focus?.();
      previousFocusRef.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (activeIndex >= filteredCommands.length) {
      setActiveIndex(Math.max(0, filteredCommands.length - 1));
    }
  }, [activeIndex, filteredCommands.length]);

  if (!open) return null;

  const runCommand = (command) => {
    if (!command) return;
    command.perform();
    onClose();
  };

  const trapFocus = (event) => {
    const focusable = Array.from(paletteRef.current?.querySelectorAll(FOCUSABLE_SELECTOR) || [])
      .filter((element) => element.getAttribute('aria-hidden') !== 'true');
    if (focusable.length === 0) return;
    const currentIndex = focusable.indexOf(document.activeElement);
    const nextIndex = event.shiftKey ? currentIndex - 1 : currentIndex + 1;
    if (nextIndex >= 0 && nextIndex < focusable.length) return;
    event.preventDefault();
    focusable[event.shiftKey ? focusable.length - 1 : 0].focus();
  };

  const handleKeyDown = (event) => {
    if (event.key === 'Tab') {
      trapFocus(event);
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((index) => Math.min(index + 1, Math.max(0, filteredCommands.length - 1)));
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      runCommand(filteredCommands[activeIndex]);
    }
  };

  return (
    <div
      className={styles.backdrop}
      role="presentation"
      data-testid="command-palette"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={paletteRef}
        className={styles.palette}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={handleKeyDown}
      >
        <input
          ref={inputRef}
          className={styles.input}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActiveIndex(0);
          }}
          placeholder="Search commands"
          aria-label="Search commands"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded="true"
          aria-controls={listboxId}
          aria-activedescendant={activeOptionId}
        />
        <div id={listboxId} className={styles.list} role="listbox" aria-label="Available commands">
          {filteredCommands.length === 0 ? (
            <div className={styles.empty}>No commands found</div>
          ) : filteredCommands.map((command, index) => (
            <button
              key={command.id}
              id={`${listboxId}-option-${command.id}`}
              type="button"
              className={`${styles.command} ${index === activeIndex ? styles.selected : ''}`}
              role="option"
              aria-selected={index === activeIndex}
              onFocus={() => setActiveIndex(index)}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => runCommand(command)}
            >
              <span className={styles.commandLabel}>{command.label}</span>
              {command.group && <span className={styles.commandGroup}>{command.group}</span>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

CommandPalette.propTypes = {
  open: PropTypes.bool.isRequired,
  commands: PropTypes.arrayOf(PropTypes.shape({
    id: PropTypes.string.isRequired,
    label: PropTypes.string.isRequired,
    group: PropTypes.string,
    keywords: PropTypes.arrayOf(PropTypes.string),
    perform: PropTypes.func.isRequired,
  })).isRequired,
  onClose: PropTypes.func.isRequired,
};
