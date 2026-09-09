import { useState, useEffect } from 'react';
import PropTypes from 'prop-types';
import { setAccent } from '../config';

const SECTIONS = [
  {
    label: 'BRONZES & COPPERS',
    swatches: [
      { hex: '#6B2E1B', name: 'Mahogany (default)' },
      { hex: '#A0552D', name: 'Persian copper' },
      { hex: '#955731', name: 'Penny patina' },
      { hex: '#9C4A1F', name: 'Burnt sienna' },
      { hex: '#B36849', name: 'Terracotta' },
      { hex: '#A8623A', name: 'Aged copper' },
      { hex: '#7B4421', name: 'Dark walnut copper' },
      { hex: '#963F1A', name: 'Rust copper' },
      { hex: '#A66C42', name: 'Brushed antique' },
      { hex: '#B87333', name: 'Classic copper' },
      { hex: '#B47545', name: 'Light brass-copper' },
      { hex: '#8B4513', name: 'Saddle brown' },
      { hex: '#C97342', name: 'Warm copper-coral' },
      { hex: '#B08D57', name: 'Antique bronze' },
      { hex: '#8B6F47', name: 'Dark bronze' },
      { hex: '#A0855B', name: 'Old bronze' },
    ],
  },
  {
    label: 'GOLDS & AMBERS',
    swatches: [
      { hex: '#D4AF37', name: 'Metallic gold' },
      { hex: '#FBBF24', name: 'Tailwind amber 400' },
      { hex: '#F59E0B', name: 'Tailwind amber 500' },
      { hex: '#B8860B', name: 'Dark goldenrod' },
      { hex: '#DAA520', name: 'Goldenrod' },
      { hex: '#C9B037', name: 'Old gold' },
      { hex: '#E6BE8A', name: 'Champagne' },
      { hex: '#CFB53B', name: 'Vegas gold' },
      { hex: '#C89B3C', name: 'League gold' },
      { hex: '#EAB308', name: 'Tailwind yellow 500' },
    ],
  },
  {
    label: 'CORAL & REDS',
    swatches: [
      { hex: '#D97757', name: 'Claude coral' },
      { hex: '#CC785C', name: 'Claude alt' },
      { hex: '#C15F3C', name: 'Claude deep' },
      { hex: '#DC6F4D', name: 'Soft coral' },
      { hex: '#B85C3F', name: 'Earthy coral' },
      { hex: '#A04A38', name: 'Brick' },
      { hex: '#EF4444', name: 'Tailwind red 500' },
      { hex: '#DC2626', name: 'Tailwind red 600' },
    ],
  },
  {
    label: 'COOL & MODERN',
    swatches: [
      { hex: '#6366F1', name: 'Indigo' },
      { hex: '#3B82F6', name: 'Tailwind blue 500' },
      { hex: '#8B5CF6', name: 'Tailwind purple 500' },
      { hex: '#14B8A6', name: 'Tailwind teal 500' },
      { hex: '#5EEAD4', name: 'Tailwind teal 300' },
      { hex: '#10B981', name: 'OpenAI green' },
      { hex: '#22C55E', name: 'Tailwind green 500' },
      { hex: '#06B6D4', name: 'Tailwind cyan 500' },
      { hex: '#EC4899', name: 'Tailwind pink 500' },
      { hex: '#0EA5E9', name: 'Tailwind sky 500' },
    ],
  },
  {
    label: 'NEUTRALS',
    swatches: [
      { hex: '#FFFFFF', name: 'Pure white' },
      { hex: '#FAFAFA', name: 'Soft white' },
      { hex: '#E5E5E5', name: 'Light gray' },
      { hex: '#94A3B8', name: 'Slate' },
      { hex: '#737373', name: 'Mid gray' },
      { hex: '#525252', name: 'Dark gray' },
    ],
  },
];

const RECENT_KEY = 'viola_accent_recent';
const RECENT_LIMIT = 8;
const DEFAULT_ACCENT = '#6B2E1B';
// Was a fixed `repeat(8, 1fr)` -- on a ~350px-wide mobile settings panel that
// divides down to ~34px square swatches, under the 44px mobile touch-target
// floor (issue #367). auto-fill + a 44px minimum keeps every swatch tappable
// on any viewport, wrapping to fewer columns (and more rows) on narrow
// screens instead of shrinking below the floor.
const SWATCH_GRID_COLUMNS = 'repeat(auto-fill, minmax(44px, 1fr))';

function readCurrentAccent() {
  if (typeof document === 'undefined') return DEFAULT_ACCENT;
  const v = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  return v || DEFAULT_ACCENT;
}

function normalizeHex(input) {
  if (typeof input !== 'string') return null;
  let s = input.trim().replace(/^#/, '');
  if (/^[0-9a-fA-F]{3}$/.test(s)) {
    s = s.split('').map((c) => c + c).join('');
  }
  if (/^[0-9a-fA-F]{6}$/.test(s)) {
    return '#' + s.toUpperCase();
  }
  return null;
}

function loadRecent() {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((h) => normalizeHex(h)) : [];
  } catch {
    return [];
  }
}

function saveRecent(arr) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(arr));
  } catch {
    /* localStorage unavailable */
  }
}

export default function AccentPicker({ onChange }) {
  const [current, setCurrent] = useState(readCurrentAccent);
  const [hexInput, setHexInput] = useState(current);
  const [recent, setRecent] = useState(loadRecent);

  useEffect(() => {
    setHexInput(current);
  }, [current]);

  const apply = (rawHex) => {
    const normalized = normalizeHex(rawHex);
    if (!normalized) return;
    setAccent(normalized);
    setCurrent(normalized);
    if (typeof onChange === 'function') onChange(normalized);
    setRecent((prev) => {
      const filtered = prev.filter((h) => h.toLowerCase() !== normalized.toLowerCase());
      const next = [normalized, ...filtered].slice(0, RECENT_LIMIT);
      saveRecent(next);
      return next;
    });
  };

  const handleHexChange = (value) => {
    setHexInput(value);
    const normalized = normalizeHex(value);
    if (normalized) apply(normalized);
  };

  const isActive = (hex) => current.toLowerCase() === hex.toLowerCase();

  const renderSwatch = ({ hex, name }) => (
    <button
      key={hex}
      type="button"
      onClick={() => apply(hex)}
      title={`${name}\n${hex.toUpperCase()}`}
      style={{
        width: '100%',
        aspectRatio: '1',
        borderRadius: 7,
        background: hex,
        border: isActive(hex) ? '2px solid #fff' : '1px solid rgba(255, 255, 255, 0.12)',
        cursor: 'pointer',
        padding: 0,
        transition: 'transform 0.12s ease',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = 'scale(1.1)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = 'scale(1)';
      }}
    />
  );

  return (
    <div
      style={{
        fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",
        color: 'rgba(255, 255, 255, 0.88)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <input
          type="color"
          value={current}
          onChange={(e) => apply(e.target.value)}
          title="Pick any color from a full spectrum"
          style={{
            // 44x44 mobile touch-target floor (was 44x38; see issue #367).
            width: 44,
            height: 44,
            border: 'none',
            borderRadius: 8,
            cursor: 'pointer',
            background: 'transparent',
            padding: 0,
          }}
        />
        <input
          type="text"
          value={hexInput}
          placeholder="Paste any #hex"
          onChange={(e) => handleHexChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') apply(hexInput);
          }}
          spellCheck={false}
          style={{
            flex: 1,
            minHeight: 44,
            boxSizing: 'border-box',
            background: 'rgba(255, 255, 255, 0.05)',
            border: '1px solid rgba(255, 255, 255, 0.1)',
            borderRadius: 8,
            color: '#fff',
            padding: '8px 12px',
            fontSize: 13,
            fontFamily: '"SF Mono", "Consolas", monospace',
            outline: 'none',
          }}
        />
      </div>
      <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 14 }}>
        Auto-applies on paste · accepts #ABC or #AABBCC · saves to your account
      </div>

      {recent.length > 0 && (
        <>
          <div
            style={{
              fontSize: 10,
              fontWeight: 700,
              opacity: 0.55,
              letterSpacing: 0.8,
              marginBottom: 6,
            }}
          >
            RECENT
          </div>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: SWATCH_GRID_COLUMNS,
              gap: 6,
              marginBottom: 14,
            }}
          >
            {recent.map((hex) => renderSwatch({ hex, name: 'Recent' }))}
          </div>
        </>
      )}

      {SECTIONS.map((section) => (
        <div key={section.label} style={{ marginBottom: 14 }}>
          <div
            style={{
              fontSize: 10,
              fontWeight: 700,
              opacity: 0.55,
              letterSpacing: 0.8,
              marginBottom: 6,
            }}
          >
            {section.label}
          </div>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: SWATCH_GRID_COLUMNS,
              gap: 6,
            }}
          >
            {section.swatches.map(renderSwatch)}
          </div>
        </div>
      ))}
    </div>
  );
}

AccentPicker.propTypes = {
  onChange: PropTypes.func,
};
