import PropTypes from 'prop-types';
import { THEME } from '../config';

// A call only gets `started_at` once its media stream connects, so a call that
// was never answered (no answer, busy, rejected, dial failure) is saved with an
// empty one — that is what rendered "Unknown date" in the call log (#3554).
// Such a record still knows when it was placed (`created_at`) and when the
// attempt finished (`ended_at`), newest field first here. "Unknown date" is
// kept for a record that genuinely carries no timestamp at all.
const CALL_TIME_FIELDS = ['started_at', 'created_at', 'ended_at'];

function callTimestamp(call) {
  for (const field of CALL_TIME_FIELDS) {
    const value = call?.[field];
    if (!value) continue;
    if (!Number.isNaN(new Date(value).getTime())) return value;
  }
  return '';
}

function formatDate(value) {
  if (!value) return 'Unknown date';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown date';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatDuration(seconds) {
  const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
  if (totalSeconds === 0) return '—';
  const minutes = Math.floor(totalSeconds / 60);
  const remainder = totalSeconds % 60;
  if (minutes === 0) return `${remainder}s`;
  return `${minutes}m ${String(remainder).padStart(2, '0')}s`;
}

function statusPillColor(status) {
  const s = (status || '').toLowerCase();
  if (s === 'completed' || s === 'saved') {
    return { fg: THEME.colors.statusGreen, bg: 'rgba(72,187,120,0.12)' };
  }
  if (s === 'failed' || s === 'error') {
    return { fg: THEME.colors.statusRed, bg: 'rgba(245,101,101,0.12)' };
  }
  if (s === 'in_progress' || s === 'active' || s === 'ringing') {
    return { fg: '#FFD27A', bg: 'rgba(255,210,122,0.12)' };
  }
  return { fg: THEME.colors.textMuted, bg: 'rgba(255,255,255,0.05)' };
}

function transcriptLines(transcriptState) {
  const transcript = transcriptState?.data?.transcript;
  return Array.isArray(transcript) ? transcript : [];
}

function transcriptRedacted(transcriptState) {
  // Server emits the literal string "[redacted]" (or "[encrypted ...]") when
  // no encryption key is configured — the transcript was deliberately not
  // persisted, not "missing."
  const value = transcriptState?.data?.transcript;
  return typeof value === 'string' && (value.startsWith('[redacted') || value.startsWith('[encrypted'));
}

function getApiKey() {
  if (typeof window === 'undefined') return '';
  return window.__VIOLA_API_KEY__ || '';
}

function recordingUrl(callId, variant = 'full') {
  // The full stereo WAV is at /v1/calls/<id>/audio (caller=L, Viola=R).
  // Mono variants live at /audio/inbound and /audio/outbound.
  const key = getApiKey();
  const qs = key ? `?api_key=${encodeURIComponent(key)}` : '';
  const tail = variant === 'full' ? 'audio' : `audio/${variant}`;
  return `/v1/calls/${encodeURIComponent(callId)}/${tail}${qs}`;
}

function Chevron({ open }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      style={{
        transition: 'transform 160ms ease',
        transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
        flexShrink: 0,
      }}
      aria-hidden="true"
    >
      <path
        d="M5 3l4 4-4 4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

Chevron.propTypes = { open: PropTypes.bool.isRequired };

export default function CallHistoryRow({ call, expanded, transcriptState, onToggle, rowRef = null }) {
  const recipient = call.phone_number || call.phone || 'Unknown recipient';
  const lines = transcriptLines(transcriptState);
  const redacted = transcriptRedacted(transcriptState);
  const pill = statusPillColor(call.status);
  const task = (call.task || '').trim();

  return (
    <div
      ref={rowRef}
      data-testid="call-history-row"
      data-call-id={call.call_id}
      style={{
        flexShrink: 0,
        border: `1px solid ${expanded ? THEME.colors.borderHover : THEME.colors.borderSubtle}`,
        borderRadius: 10,
        backgroundColor: expanded ? THEME.colors.bgElevated : THEME.colors.bgCard,
        overflow: 'hidden',
        transition: 'background-color 120ms ease, border-color 120ms ease',
      }}
    >
      <button
        type="button"
        aria-expanded={expanded}
        onClick={onToggle}
        style={{
          width: '100%',
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
          padding: '16px 18px',
          border: 'none',
          background: 'transparent',
          color: THEME.colors.textPrimary,
          textAlign: 'left',
          cursor: 'pointer',
          font: 'inherit',
        }}
      >
        {/* Primary row: chevron + recipient + status pill */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            color: THEME.colors.textBright,
          }}
        >
          <Chevron open={expanded} />
          <span
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: 16,
              fontWeight: 650,
              fontVariantNumeric: 'tabular-nums',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {recipient}
          </span>
          <span
            style={{
              flexShrink: 0,
              padding: '3px 10px',
              borderRadius: 999,
              fontSize: 11,
              fontWeight: 600,
              letterSpacing: 0.3,
              textTransform: 'uppercase',
              color: pill.fg,
              backgroundColor: pill.bg,
            }}
          >
            {call.status || 'saved'}
          </span>
        </div>

        {/* Task line (if present) */}
        {task && (
          <div
            style={{
              marginLeft: 24,
              color: THEME.colors.textPrimary,
              fontSize: 13,
              lineHeight: 1.4,
              overflow: 'hidden',
              display: '-webkit-box',
              WebkitBoxOrient: 'vertical',
              WebkitLineClamp: 2,
            }}
          >
            {task}
          </div>
        )}

        {/* Meta line: date · duration · recording */}
        <div
          style={{
            marginLeft: 24,
            display: 'flex',
            alignItems: 'center',
            gap: 14,
            color: THEME.colors.textMuted,
            fontSize: 12,
          }}
        >
          <span>{formatDate(callTimestamp(call))}</span>
          <span style={{ opacity: 0.4 }}>·</span>
          <span style={{ fontVariantNumeric: 'tabular-nums' }}>
            {formatDuration(call.duration_seconds)}
          </span>
          {call.has_recording && (
            <>
              <span style={{ opacity: 0.4 }}>·</span>
              <span
                style={{
                  color: THEME.colors.statusGreen,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    backgroundColor: 'currentColor',
                    display: 'inline-block',
                  }}
                />
                Recording
              </span>
            </>
          )}
        </div>
      </button>

      {expanded && (
        <div
          data-testid="call-history-transcript"
          style={{
            borderTop: `1px solid ${THEME.colors.borderSubtle}`,
            padding: '14px 18px 18px 42px',
            color: THEME.colors.textPrimary,
            fontSize: 14,
            lineHeight: 1.55,
          }}
        >
          {call.summary && (
            <div
              style={{
                marginBottom: 14,
                padding: '10px 12px',
                borderRadius: 8,
                backgroundColor: 'rgba(255,255,255,0.04)',
                color: THEME.colors.textPrimary,
                fontSize: 13,
                lineHeight: 1.5,
              }}
            >
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: 0.4,
                  textTransform: 'uppercase',
                  color: THEME.colors.textMuted,
                  marginBottom: 4,
                }}
              >
                Summary
              </div>
              {call.summary}
            </div>
          )}
          {call.has_recording && (
            <div
              style={{
                marginBottom: 14,
                padding: '10px 12px 12px',
                borderRadius: 8,
                backgroundColor: 'rgba(255,255,255,0.04)',
                border: `1px solid ${THEME.colors.borderSubtle}`,
              }}
            >
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: 0.4,
                  textTransform: 'uppercase',
                  color: THEME.colors.textMuted,
                  marginBottom: 6,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                <span>Recording</span>
                <span style={{ color: THEME.colors.statusGreen, fontSize: 10 }}>
                  caller (L) · Viola (R)
                </span>
              </div>
              <div
                style={{
                  padding: 8,
                  borderRadius: 10,
                  backgroundColor: THEME.colors.bgCard,
                  border: `1px solid ${THEME.colors.borderSubtle}`,
                }}
              >
                <audio
                  aria-label={`Recording for ${recipient}`}
                  controls
                  preload="none"
                  src={recordingUrl(call.call_id, 'full')}
                  style={{
                    display: 'block',
                    width: '100%',
                    height: 36,
                    borderRadius: 999,
                    backgroundColor: THEME.colors.bgCard,
                    colorScheme: 'dark',
                    accentColor: THEME.colors.statusGreen,
                  }}
                />
              </div>
            </div>
          )}
          {transcriptState?.loading ? (
            <div style={{ color: THEME.colors.textMuted, padding: '4px 0' }}>Loading transcript…</div>
          ) : transcriptState?.error ? (
            <div style={{ color: THEME.colors.statusRed, padding: '4px 0' }}>Transcript unavailable.</div>
          ) : redacted ? (
            <div
              style={{
                padding: '10px 12px',
                borderRadius: 8,
                border: `1px dashed ${THEME.colors.borderSubtle}`,
                color: THEME.colors.textMuted,
                fontSize: 13,
                lineHeight: 1.5,
              }}
            >
              <div style={{ fontWeight: 600, color: THEME.colors.textPrimary, marginBottom: 4 }}>
                Transcript redacted at rest
              </div>
              Set <code style={{ fontFamily: 'monospace', fontSize: 12 }}>VIOLA_MEMORY_ENCRYPTION_KEY</code> in your environment so future call transcripts are stored encrypted. Calls saved before the key was configured cannot be recovered.
            </div>
          ) : lines.length === 0 ? (
            <div style={{ color: THEME.colors.textMuted, fontStyle: 'italic', padding: '4px 0' }}>
              No transcript saved for this call.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {lines.map((entry, index) => {
                const role = (entry.role || 'line').toLowerCase();
                const isViola = role === 'assistant' || role === 'viola' || role === 'us';
                return (
                  <div
                    key={`${entry.ts || role}-${index}`}
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      // Same own-side-right convention as the live call transcript
                      // (PhoneCallPanel): Viola (own side) right, them left.
                      alignItems: isViola ? 'flex-end' : 'flex-start',
                    }}
                  >
                    <div
                      style={{
                        fontSize: 10,
                        fontWeight: 600,
                        letterSpacing: 0.4,
                        textTransform: 'uppercase',
                        color: THEME.colors.textMuted,
                        marginBottom: 3,
                      }}
                    >
                      {isViola ? 'Viola' : entry.role || 'them'}
                    </div>
                    <div
                      style={{
                        maxWidth: '80%',
                        padding: '8px 12px',
                        borderRadius: 10,
                        backgroundColor: isViola
                          ? 'rgba(125,150,255,0.10)'
                          : 'rgba(255,255,255,0.05)',
                        color: THEME.colors.textPrimary,
                        overflowWrap: 'anywhere',
                      }}
                    >
                      {entry.text || ''}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

CallHistoryRow.propTypes = {
  call: PropTypes.shape({
    call_id: PropTypes.string.isRequired,
    phone_number: PropTypes.string,
    phone: PropTypes.string,
    task: PropTypes.string,
    status: PropTypes.string,
    duration_seconds: PropTypes.number,
    created_at: PropTypes.string,
    started_at: PropTypes.string,
    ended_at: PropTypes.string,
    summary: PropTypes.string,
    has_recording: PropTypes.bool,
  }).isRequired,
  expanded: PropTypes.bool.isRequired,
  transcriptState: PropTypes.shape({
    loading: PropTypes.bool,
    error: PropTypes.string,
    data: PropTypes.shape({
      transcript: PropTypes.arrayOf(PropTypes.shape({
        role: PropTypes.string,
        text: PropTypes.string,
        ts: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
      })),
    }),
  }),
  onToggle: PropTypes.func.isRequired,
  rowRef: PropTypes.func,
};
