import React, { useState, useRef, useEffect, useCallback } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A fully custom range slider with label and value display.
 * @param {Object} props
 * @param {number} props.value - Current slider value
 * @param {function} props.onChange - Called with new numeric value
 * @param {number} [props.min=0] - Minimum value
 * @param {number} [props.max=100] - Maximum value
 * @param {string} [props.label] - Label text shown above slider
 * @param {number} [props.step=1] - Step increment
 * @param {string} [props.tooltip] - Tooltip text
 */
const Slider = React.memo(({ value, onChange, min = 0, max = 100, label, step = 1, tooltip }) => {
  const trackRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);

  const percentage = ((value - min) / (max - min)) * 100;

  const updateValue = useCallback((clientX) => {
    if (!trackRef.current) return;
    const rect = trackRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(clientX - rect.left, rect.width));
    const newPercentage = x / rect.width;
    let newValue = min + newPercentage * (max - min);
    // Round to step
    newValue = Math.round(newValue / step) * step;
    // Rounding to a step that doesn't evenly divide (max - min) can push the
    // value past either end (e.g. max=10, step=4 -> 12) — clamp back in range.
    newValue = Math.max(min, Math.min(max, newValue));
    onChange(newValue);
  }, [min, max, step, onChange]);

  const handleMouseDown = (e) => {
    setIsDragging(true);
    updateValue(e.clientX);
  };

  const handleTouchStart = (e) => {
    setIsDragging(true);
    updateValue(e.touches[0].clientX);
  };

  const handleTouchMove = (e) => {
    e.preventDefault();
    updateValue(e.touches[0].clientX);
  };

  useEffect(() => {
    if (!isDragging) return;

    const handleMouseMove = (e) => updateValue(e.clientX);
    const handleMouseUp = () => setIsDragging(false);
    const handleTouchEnd = () => setIsDragging(false);

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    document.addEventListener('touchend', handleTouchEnd);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.removeEventListener('touchend', handleTouchEnd);
    };
  }, [isDragging, updateValue]);

  // Format display value
  const displayValue = step < 1 ? value.toFixed(1) : value;

  return (
    <div style={{ width: '100%' }} title={tooltip}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
        <span
          style={{ color: theme.colors.textSecondary, fontSize: '14px' }}
          title={tooltip}
        >
          {label}
        </span>
        <span style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 500 }}>{displayValue}</span>
      </div>
      <div
        ref={trackRef}
        data-hold-interactive
        onMouseDown={handleMouseDown}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        title={tooltip}
        style={{
          position: 'relative',
          height: '44px',
          display: 'flex',
          alignItems: 'center',
          cursor: 'pointer',
          touchAction: 'none',
          // Drag must never begin an iOS text selection or callout.
          WebkitUserSelect: 'none',
          userSelect: 'none',
          WebkitTouchCallout: 'none',
        }}
      >
        {/* Track background */}
        <div style={{
          position: 'absolute',
          width: '100%',
          height: '4px',
          borderRadius: '2px',
          backgroundColor: theme.colors.glassActive,
        }} />
        {/* Track fill */}
        <div style={{
          position: 'absolute',
          width: `${percentage}%`,
          height: '4px',
          borderRadius: '2px',
          backgroundColor: theme.colors.accent,
          transition: isDragging ? 'none' : 'width 0.1s ease',
        }} />
        {/* Thumb */}
        <div style={{
          position: 'absolute',
          left: `calc(${percentage}% - 10px)`,
          width: '20px',
          height: '20px',
          borderRadius: '50%',
          backgroundColor: theme.colors.textBright || '#ffffff',
          boxShadow: `0 2px 6px ${theme.colors.shadowLight}`,
          transition: isDragging ? 'none' : 'left 0.1s ease',
        }} />
      </div>
    </div>
  );
});

Slider.propTypes = {
  value: PropTypes.number.isRequired,
  onChange: PropTypes.func.isRequired,
  min: PropTypes.number,
  max: PropTypes.number,
  label: PropTypes.string,
  step: PropTypes.number,
  tooltip: PropTypes.string,
};

export default Slider;
