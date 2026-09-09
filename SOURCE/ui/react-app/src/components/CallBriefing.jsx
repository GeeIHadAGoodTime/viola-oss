/**
 * CallBriefing — Shows pre-call plan for user approval.
 *
 * Displays the phone number, objective, and talking points.
 * User can approve or modify the plan before dialing.
 */
import React from 'react';
import { THEME } from '../config';

const theme = THEME;

export default function CallBriefing({ briefing, onDismiss }) {
  if (!briefing) return null;

  const { phone_number, business_name, objective, talking_points, approved } = briefing;

  return (
    <div
      style={{
        position: 'fixed',
        bottom: 20,
        right: 20,
        width: 380,
        background: '#1a1a2e',
        border: `1px solid ${approved ? theme.colors.statusGreen : theme.colors.statusYellow}`,
        borderRadius: 12,
        padding: 16,
        zIndex: 9998,
        boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: approved ? theme.colors.statusGreen : theme.colors.statusYellow, fontWeight: 600, fontSize: 13 }}>
          {approved ? 'Call approved' : 'Viola needs your approval'}
        </span>
        <button
          onClick={onDismiss}
          style={{
            background: 'none',
            border: 'none',
            color: '#888',
            cursor: 'pointer',
            fontSize: 16,
          }}
        >
          x
        </button>
      </div>

      <div style={{ color: '#ccc', fontSize: 13, marginBottom: 4 }}>
        Call: <strong style={{ color: '#eee' }}>{business_name || phone_number}</strong>
      </div>

      <div style={{ color: '#ccc', fontSize: 13, marginBottom: 8 }}>
        What Viola will do: {objective}
      </div>

      {talking_points && talking_points.length > 0 && (
        <ul style={{ color: '#aaa', fontSize: 12, paddingLeft: 20, margin: '4px 0' }}>
          {talking_points.map((pt, i) => (
            <li key={i} style={{ marginBottom: 2 }}>
              {pt}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
