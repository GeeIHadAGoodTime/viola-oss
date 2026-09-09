/**
 * DropdownMenu — Navigation menu with ARIA roles and keyboard support.
 */
import { useRef } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './DropdownMenu.module.css';

const DropdownMenu = ({ isOpen, onClose, onOpenHistory, onOpenQueue, onOpenSettings, onOpenRooms, onOpenHelp }) => {
  const itemRefs = useRef([]);

  if (!isOpen) return null;

  const menuItems = [
    { icon: 'history', label: 'Chat History', onClick: onOpenHistory },
    { icon: 'queue', label: 'Queue', onClick: onOpenQueue },
    { icon: 'rooms', label: 'Rooms', onClick: onOpenRooms },
    { icon: 'settings', label: 'Settings', onClick: onOpenSettings },
    { icon: 'help', label: 'Help & Guide', onClick: onOpenHelp },
  ];

  const icons = {
    history: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>,
    queue: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><circle cx="4" cy="6" r="1" fill="currentColor" /><circle cx="4" cy="12" r="1" fill="currentColor" /><circle cx="4" cy="18" r="1" fill="currentColor" /></svg>,
    rooms: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><rect x="4" y="2" width="16" height="20" rx="2"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="6" r="1"/></svg>,
    settings: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><circle cx="12" cy="12" r="3" /><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" /></svg>,
    help: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><circle cx="12" cy="17" r="0.5" fill="currentColor" /></svg>,
  };

  const handleKeyDown = (e, idx) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose?.();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = (idx + 1) % menuItems.length;
      itemRefs.current[next]?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prev = (idx - 1 + menuItems.length) % menuItems.length;
      itemRefs.current[prev]?.focus();
    } else if (e.key === 'Tab') {
      onClose?.();
    }
  };

  return (
    <div
      role="menu"
      aria-label="Navigation menu"
      className={styles.menu}
      style={{
        backgroundColor: `${THEME.colors.bgCard}F8`,
        boxShadow: `0 16px 48px ${THEME.colors.shadowHeavy}, inset 0 0 0 1px ${THEME.colors.glassBase}`,
      }}
    >
      {menuItems.map((item, idx) => (
        <button
          key={item.icon}
          ref={el => { itemRefs.current[idx] = el; }}
          role="menuitem"
          onClick={item.onClick}
          aria-label={item.label}
          onKeyDown={(e) => handleKeyDown(e, idx)}
          className={styles.menuItem}
          style={{ color: THEME.colors.textSecondary }}
        >
          {icons[item.icon]}
          {item.label}
        </button>
      ))}
    </div>
  );
};

DropdownMenu.propTypes = {
  isOpen: PropTypes.bool,
  onClose: PropTypes.func,
  onOpenHistory: PropTypes.func,
  onOpenQueue: PropTypes.func,
  onOpenSettings: PropTypes.func,
  onOpenRooms: PropTypes.func,
  onOpenHelp: PropTypes.func,
};

export default DropdownMenu;
