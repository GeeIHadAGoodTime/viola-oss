// Viola Dark Theme - Design Tokens
// Auto-extracted from smart-display-v7.jsx

export const violaDark = {
  colors: {
    // Backgrounds
    bgVoid: '#000000',
    bgCard: '#0d0d0d',
    bgElevated: '#1a1a1a',

    // Text hierarchy (use these consistently)
    textPrimary: 'rgba(255,255,255,0.85)',
    textSecondary: 'rgba(255,255,255,0.65)',
    textTertiary: 'rgba(255,255,255,0.55)',
    textMuted: 'rgba(255,255,255,0.45)',
    textDisabled: 'rgba(255,255,255,0.35)',
    textFaint: 'rgba(255,255,255,0.2)',

    // Status
    statusGreen: '#22c55e',
    statusGreenGlow: 'rgba(34, 197, 94, 0.4)',
    statusYellow: '#eab308',
    statusYellowGlow: 'rgba(234, 179, 8, 0.4)',
    statusRed: '#ef4444',
    statusRedGlow: 'rgba(239, 68, 68, 0.4)',

    // Borders
    borderSubtle: 'rgba(255,255,255,0.04)',
    borderLight: 'rgba(255,255,255,0.06)',
    borderHover: 'rgba(255,255,255,0.12)',
    divider: 'rgba(255,255,255,0.08)',

    // Glass
    glassBase: 'rgba(255,255,255,0.07)',
    glassHover: 'rgba(255,255,255,0.10)',
    glassActive: 'rgba(255,255,255,0.14)',
  },

  gradients: {
    glass: 'linear-gradient(145deg, rgba(255,255,255,0.07) 0%, rgba(255,255,255,0.02) 100%)',
    glassHover: 'linear-gradient(145deg, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0.04) 100%)',
    glassActive: 'linear-gradient(145deg, rgba(255,255,255,0.14) 0%, rgba(255,255,255,0.06) 100%)',
    divider: 'linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.08) 15%, rgba(255,255,255,0.08) 85%, transparent 100%)',
  },

  typography: {
    fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",

    weights: {
      ultraLight: 200,
      light: 300,
      regular: 400,
      medium: 500,
    },

    // Responsive sizes using clamp
    sizes: {
      displayXL: 'clamp(72px, 12vw, 108px)',
      displayLG: 'clamp(36px, 6vw, 54px)',
      h1: 'clamp(32px, 5vw, 52px)',
      h2: 'clamp(18px, 2.8vw, 28px)',
      h3: 'clamp(18px, 2.5vw, 24px)',
      bodyLG: 'clamp(17px, 2.2vw, 22px)',
      body: 'clamp(15px, 2vw, 19px)',
      bodySM: 'clamp(14px, 2vw, 18px)',
      caption: '9px',
    },
  },

  spacing: {
    containerPadding: 'clamp(32px, 4.5vw, 56px) clamp(36px, 5.5vw, 72px)',
    sectionGap: 'clamp(20px, 3vh, 32px)',
    elementGap: 'clamp(16px, 2.5vh, 28px)',
  },

  radii: {
    card: '32px',
    buttonLG: '50%',
    buttonSM: '16px',
    albumArt: '16px',
    menu: '16px',
    progress: '2px',
  },

  shadows: {
    cardInset: 'inset 0 0 0 1px rgba(255,255,255,0.04)',
    albumArt: '0 12px 48px rgba(0,0,0,0.5)',
    menu: '0 16px 48px rgba(0,0,0,0.65), inset 0 0 0 1px rgba(255,255,255,0.07)',
    statusGlow: (color) => `0 0 20px ${color}`,
    statusActiveGlow: '0 0 28px rgba(255,255,255,0.35)',
  },

  transitions: {
    default: 'all 0.2s ease',
    fast: 'all 0.15s ease',
    progress: 'width 0.3s linear',
  },

  backdrop: {
    blur: 'blur(24px)',
  },
};

// Status color helper
export const getStatusColors = (status) => ({
  listening: { ring: violaDark.colors.statusGreen, glow: violaDark.colors.statusGreenGlow },
  starting: { ring: violaDark.colors.statusYellow, glow: violaDark.colors.statusYellowGlow },
  off: { ring: violaDark.colors.statusRed, glow: violaDark.colors.statusRedGlow },
})[status] || { ring: violaDark.colors.statusRed, glow: violaDark.colors.statusRedGlow };

export default violaDark;
