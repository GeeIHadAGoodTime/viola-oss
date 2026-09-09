/**
 * AuthLoadingScreen — branded placeholder shown while useAuth() resolves the
 * initial session (status === 'loading').
 *
 * Visually consistent with main.jsx's BrandedLoader so there is no jarring
 * swap between backend-readiness loading and auth-session loading.
 */
import React from 'react';
import { pageStyle } from './authStyles';
import { THEME } from '../../config';

export default function AuthLoadingScreen() {
  return (
    <div style={pageStyle()} role="status" aria-live="polite" data-testid="auth-loading">
      <style>{`
        @keyframes viola-auth-pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(0.86); }
        }
      `}</style>
      <div style={{ display: 'grid', gap: '20px', justifyItems: 'center' }}>
        <div
          style={{
            width: '88px',
            height: '88px',
            borderRadius: '50%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: `linear-gradient(135deg, ${THEME.colors.accentGlow} 0%, transparent 100%)`,
            animation: 'viola-auth-pulse 2s ease-in-out infinite',
          }}
        >
          <img
            src={`${import.meta.env.BASE_URL || '/'}viola_icon.png`}
            alt=""
            width="56"
            height="56"
          />
        </div>
        <span style={{ fontSize: '15px', fontWeight: 500, color: THEME.colors.textSecondary }}>
          Connecting to your account…
        </span>
      </div>
    </div>
  );
}
