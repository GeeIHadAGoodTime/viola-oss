import React, { useState, useRef, useEffect } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { Icons } from './Icons';

const theme = THEME;

/**
 * A fully custom dropdown select component (no native select element).
 * @param {Object} props
 * @param {string|number} props.value - Currently selected value
 * @param {function} props.onChange - Called with new selected value
 * @param {Array<{value: string|number, label: string}>} props.options - Available options
 * @param {string} [props.label] - Label text shown above dropdown
 * @param {string} [props.tooltip] - Tooltip text
 */
const Select = React.memo(({ value, onChange, options, label, tooltip }) => {
  const [isOpen, setIsOpen] = useState(false);
  const [hoveredIndex, setHoveredIndex] = useState(-1);
  const containerRef = useRef(null);
  const listboxId = React.useId();

  const selectedOption = options.find(opt => opt.value === value);

  // Close on outside click
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Close on escape
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, []);

  return (
    <div style={{ width: '100%' }} ref={containerRef}>
      {label && (
        <label style={{
          display: 'block',
          marginBottom: '8px',
          color: theme.colors.textSecondary,
          fontSize: '14px',
        }}
        title={tooltip}
        >
          {label}
        </label>
      )}
      <div style={{ position: 'relative' }}>
        {/* Trigger Button */}
        <button
          type="button"
          onClick={() => setIsOpen(!isOpen)}
          aria-label={label ? `${label}: ${selectedOption?.label || 'Select...'}` : `Select ${selectedOption?.label || 'option'}`}
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          aria-controls={listboxId}
          title={tooltip}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              setIsOpen(true);
            }
          }}
          style={{
            width: '100%',
            minHeight: '44px',
            padding: '12px 16px',
            borderRadius: '12px',
            border: `1px solid ${isOpen ? theme.colors.accentBorder : theme.colors.borderLight}`,
            backgroundColor: theme.colors.bgElevated,
            color: theme.colors.textPrimary,
            fontSize: '14px',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            transition: 'all 0.15s ease',
            outline: 'none',
            boxShadow: isOpen ? `0 0 0 3px ${theme.colors.accentRing}` : 'none',
            textAlign: 'left',
          }}
        >
          <span>{selectedOption?.label || 'Select...'}</span>
          <div style={{
            transform: isOpen ? 'rotate(180deg)' : 'rotate(0deg)',
            transition: 'transform 0.2s ease',
            color: theme.colors.textMuted,
          }}>
            <Icons.ChevronDown />
          </div>
        </button>

        {/* Dropdown Menu */}
        {isOpen && (
          <div
            id={listboxId}
            role="listbox"
            aria-label={label || 'Options'}
            className="viola-scrollbar"
            style={{
              position: 'absolute',
              top: 'calc(100% + 4px)',
              left: 0,
              right: 0,
              backgroundColor: theme.colors.bgElevated,
              border: `1px solid ${theme.colors.borderLight}`,
              borderRadius: '12px',
              boxShadow: `0 16px 48px ${theme.colors.shadowMedium}`,
              zIndex: 1000,
              overflow: 'hidden',
              maxHeight: '200px',
              overflowY: 'auto',
            }}
          >
            {options.map((option, index) => {
              const isSelected = option.value === value;
              const isHovered = hoveredIndex === index;

              return (
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  key={option.value}
                  onClick={() => {
                    onChange(option.value);
                    setIsOpen(false);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      e.preventDefault();
                      setIsOpen(false);
                    }
                  }}
                  onMouseEnter={() => setHoveredIndex(index)}
                  onMouseLeave={() => setHoveredIndex(-1)}
                  style={{
                    width: '100%',
                    minHeight: '44px',
                    border: 'none',
                    padding: '12px 16px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    backgroundColor: isSelected
                      ? theme.colors.accentSubtle
                      : isHovered ? theme.colors.accentHover : 'transparent',
                    color: isSelected ? theme.colors.accent : theme.colors.textSecondary,
                    transition: 'background-color 0.1s ease',
                    fontFamily: 'inherit',
                    textAlign: 'left',
                  }}
                >
                  <span style={{ fontSize: '14px' }}>{option.label}</span>
                  {isSelected && (
                    <div style={{ color: theme.colors.accent }}>
                      <Icons.Check />
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
});

Select.propTypes = {
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
  onChange: PropTypes.func.isRequired,
  options: PropTypes.arrayOf(PropTypes.shape({
    value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
    label: PropTypes.string.isRequired,
  })).isRequired,
  label: PropTypes.string,
  tooltip: PropTypes.string,
};

export default Select;
