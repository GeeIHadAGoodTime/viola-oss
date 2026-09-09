/* eslint react/jsx-uses-vars: "error" */
import PropTypes from 'prop-types';
import styles from './PillBar.module.css';

function ChatIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4.5 5.5h15v10h-9l-4.5 3.5v-3.5h-1.5z" />
    </svg>
  );
}

function MusicIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M9 18.5a2.5 2.5 0 1 1-2-2.45v-10.3l10-2v11.25a2.5 2.5 0 1 1-2-2.45v-6.8l-8 1.6v8.7a2.5 2.5 0 0 1 2 2.45z" />
    </svg>
  );
}

function PhoneIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M21.4 16.9v2.8a1.8 1.8 0 0 1-2 1.8 19.4 19.4 0 0 1-8.5-3 19.1 19.1 0 0 1-5.9-5.9 19.4 19.4 0 0 1-3-8.5 1.8 1.8 0 0 1 1.8-2h2.8a1.8 1.8 0 0 1 1.8 1.5c.1.9.4 1.8.7 2.7a1.8 1.8 0 0 1-.4 1.9l-1.2 1.2a15.6 15.6 0 0 0 5.9 5.9l1.2-1.2a1.8 1.8 0 0 1 1.9-.4c.9.3 1.8.6 2.7.7a1.8 1.8 0 0 1 1.5 1.8z" />
    </svg>
  );
}

function GlobeIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3c2.4 2.6 3.6 5.6 3.6 9s-1.2 6.4-3.6 9c-2.4-2.6-3.6-5.6-3.6-9s1.2-6.4 3.6-9z" />
    </svg>
  );
}

const ICONS = {
  chat: ChatIcon,
  music: MusicIcon,
  phone: PhoneIcon,
  globe: GlobeIcon,
};

function Pill({ item, active, onSelect, iconOnly }) {
  const Icon = ICONS[item.icon] || ChatIcon;
  const hideLabel = iconOnly && !item.contextual;
  return (
    <button
      type="button"
      className={`${styles.pill} ${active ? styles.active : ''} ${item.contextual ? styles.contextual : ''} ${hideLabel ? styles.iconOnly : ''}`}
      aria-pressed={active}
      aria-label={item.ariaLabel || item.label}
      title={item.title || item.label}
      data-testid={`stage-pill-${item.id}`}
      onClick={() => onSelect(item.id)}
    >
      <span className={styles.iconWrap}>
        <Icon />
        {item.statusDot && <span className={`${styles.statusDot} ${item.pulse ? styles.pulse : ''}`} />}
      </span>
      {!hideLabel && <span className={styles.label}>{item.label}</span>}
      {item.meta && <span className={styles.meta}>{item.meta}</span>}
    </button>
  );
}

Pill.propTypes = {
  item: PropTypes.shape({
    id: PropTypes.string.isRequired,
    label: PropTypes.string.isRequired,
    ariaLabel: PropTypes.string,
    title: PropTypes.string,
    icon: PropTypes.string,
    contextual: PropTypes.bool,
    statusDot: PropTypes.bool,
    pulse: PropTypes.bool,
    meta: PropTypes.string,
  }).isRequired,
  active: PropTypes.bool.isRequired,
  onSelect: PropTypes.func.isRequired,
  iconOnly: PropTypes.bool,
};

export default function PillBar({
  activeMode,
  pinnedItems,
  contextualItems = [],
  onSelect,
  iconOnlyPinned = false,
  trailing = null,
}) {
  const liveStatus = [
    ...pinnedItems
      .filter((item) => item.statusDot || item.meta)
      .map((item) => `${item.label}${item.meta ? ` ${item.meta}` : ''}`),
    ...contextualItems.map((item) => `${item.label} available`),
  ].join('. ');

  return (
    <nav className={styles.shell} aria-label="Stage modes" data-testid="stage-pill-bar">
      <span
        className={styles.liveRegion}
        aria-live="polite"
        aria-atomic="true"
        data-testid="stage-pill-status"
      >
        {liveStatus}
      </span>
      <div className={styles.group}>
        {pinnedItems.map((item) => (
          <Pill
            key={item.id}
            item={item}
            active={activeMode === item.id}
            onSelect={onSelect}
            iconOnly={iconOnlyPinned}
          />
        ))}
        {trailing}
      </div>

      {contextualItems.length > 0 && (
        <div className={`${styles.group} ${styles.contextualGroup}`}>
          {contextualItems.map((item) => (
            <Pill
              key={item.id}
              item={{ ...item, contextual: true }}
              active={activeMode === item.id || Boolean(item.activeAliases?.includes(activeMode))}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </nav>
  );
}

PillBar.propTypes = {
  activeMode: PropTypes.string.isRequired,
  pinnedItems: PropTypes.arrayOf(PropTypes.object).isRequired,
  contextualItems: PropTypes.arrayOf(PropTypes.object),
  onSelect: PropTypes.func.isRequired,
  iconOnlyPinned: PropTypes.bool,
  trailing: PropTypes.node,
};
