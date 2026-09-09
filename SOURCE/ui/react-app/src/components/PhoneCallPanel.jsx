/* eslint react/jsx-uses-vars: "error" */
import { useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';

const SUGGESTED_ACTIONS = [
  'Refuse upsell',
  'Approve booking',
  'Ask for confirmation number',
  'End call politely',
  'Need more info',
];

const RECIPIENT_STATE_LABELS = {
  voicemail: 'Voicemail',
  ivr: 'IVR',
  human: 'Human',
  hold: 'Hold',
};

function parseStartedAt(startedAt, fallbackMs) {
  if (typeof startedAt === 'number') {
    return startedAt > 1000000000000 ? startedAt : startedAt * 1000;
  }
  if (typeof startedAt === 'string' && startedAt.trim()) {
    const parsed = Date.parse(startedAt);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallbackMs;
}

function formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
  const seconds = String(totalSeconds % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}

function formatTask(task) {
  if (!task) return '';
  if (typeof task === 'string') return task;
  return task.description || task.title || task.summary || '';
}

function formatCost(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '';
  return `$${numeric.toFixed(2)}`;
}

function normalizeRecipientState(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return RECIPIENT_STATE_LABELS[normalized] ? normalized : 'human';
}

function transcriptSpeakerLabel(role, recipientName) {
  if (role === 'viola') return 'Viola';
  if (role === 'them') return recipientName || 'Them';
  return 'System';
}

function transcriptBubbleStyle(role, partial) {
  const isViola = role === 'viola';
  const isSystem = role === 'system';
  return {
    maxWidth: isSystem ? '100%' : '82%',
    padding: isSystem ? '8px 10px' : '9px 12px',
    borderRadius: 10,
    backgroundColor: isSystem
      ? 'rgba(255,255,255,0.035)'
      : isViola
        ? 'rgba(125,150,255,0.10)'
        : 'rgba(255,255,255,0.055)',
    border: partial
      ? `1px solid ${THEME.colors.statusGreen}55`
      : `1px solid ${isSystem ? THEME.colors.borderSubtle : 'transparent'}`,
    boxShadow: partial ? `0 0 0 1px ${THEME.colors.statusGreen}18` : 'none',
    color: isSystem ? THEME.colors.textMuted : THEME.colors.textPrimary,
    overflowWrap: 'anywhere',
  };
}

function PhoneIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M6.6 10.8c1.6 3.2 3.4 5 6.6 6.6l2.2-2.2c.3-.3.8-.4 1.2-.3 1.3.4 2.6.6 4 .6.8 0 1.4.6 1.4 1.4v3.5c0 .8-.6 1.4-1.4 1.4C10.3 22.3 1.7 13.7 1.7 3.4 1.7 2.6 2.3 2 3.1 2h3.5C7.4 2 8 2.6 8 3.4c0 1.4.2 2.8.6 4 .1.4 0 .8-.3 1.2l-1.7 2.2z"
        fill="currentColor"
      />
    </svg>
  );
}

function HistoryIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M3 12a9 9 0 1 0 3-6.7" />
      <path d="M3 4v5h5" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

export default function PhoneCallPanel({
  callId,
  callMeta = {},
  onEndCall,
  isListening,
  transcripts,
  takeoverActive,
  takeoverPending = false,
  onToggleListen,
  onToggleTakeover,
  onActivateTakeover,
  onSendOperatorMessage,
  onOpenHistory = () => {},
  activeConsultation = null,
  onConsultationReply = () => {},
  onConsultationTakeover = () => {},
  queuedCalls = [],
  onRemoveQueuedCall = () => {},
  recordingActive = false,
}) {
  const paneRef = useRef(null);
  const fallbackStartRef = useRef(Date.now());
  const [nowMs, setNowMs] = useState(Date.now());
  const [operatorText, setOperatorText] = useState('');
  const [consultAnswer, setConsultAnswer] = useState('');
  const startedMs = useMemo(
    () => parseStartedAt(callMeta.started_at, fallbackStartRef.current),
    [callMeta.started_at]
  );
  const phoneNumber = callMeta.phone_number || callMeta.phone || callMeta.to || 'phone call';
  const recipientName = callMeta.recipient_name || callMeta.business_name || phoneNumber;
  const taskText = formatTask(callMeta.task);
  const elapsedLabel = formatElapsed(nowMs - startedMs);
  const costLabel = formatCost(
    callMeta.current_cost_usd
    ?? callMeta.cost_usd
    ?? callMeta.estimated_cost_usd
  );
  const recipientState = normalizeRecipientState(callMeta.recipient_state);
  const recipientLabel = RECIPIENT_STATE_LABELS[recipientState];

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [callId]);

  useEffect(() => {
    if (paneRef.current) {
      paneRef.current.scrollTop = paneRef.current.scrollHeight;
    }
  }, [transcripts]);

  useEffect(() => {
    setConsultAnswer('');
  }, [activeConsultation?.call_id, activeConsultation?.question]);

  const handleOperatorSubmit = (event) => {
    event.preventDefault();
    const message = operatorText.trim();
    if (!message) return;
    void onSendOperatorMessage(message);
    setOperatorText('');
  };

  const handleSuggestedAction = (message) => {
    setOperatorText(message);
    void onSendOperatorMessage(message);
  };

  const handleOperatorKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      handleOperatorSubmit(event);
    }
  };

  const handleConsultSubmit = (event) => {
    event.preventDefault();
    const answer = consultAnswer.trim();
    if (!answer || !activeConsultation) return;
    onConsultationReply(activeConsultation.call_id || callId, answer);
    setConsultAnswer('');
  };

  return (
    <section
      aria-label="Active phone call"
      data-testid="phone-call-panel"
      style={{
        display: 'flex',
        flexDirection: 'column',
        flex: 1,
        minHeight: 0,
        overflow: 'hidden',
        marginTop: 'clamp(8px, 1.5vh, 16px)',
        marginBottom: 'clamp(8px, 1.5vh, 14px)',
        padding: 12,
        borderRadius: 12,
        backgroundColor: THEME.colors.bgElevated,
        border: `1px solid ${THEME.colors.borderLight}`,
        boxShadow: `0 18px 60px ${THEME.colors.shadowMedium}`,
        color: THEME.colors.textPrimary,
      }}
    >
      <header style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 10, flexShrink: 0 }}>
        <div
          style={{
            width: 36,
            height: 36,
            borderRadius: 12,
            display: 'grid',
            placeItems: 'center',
            backgroundColor: `${THEME.colors.statusGreen}22`,
            color: THEME.colors.statusGreen,
            flexShrink: 0,
          }}
        >
          <PhoneIcon />
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0, flexWrap: 'wrap' }}>
            <span
              data-testid="phone-live-badge"
              style={{
                color: THEME.colors.bgVoid,
                backgroundColor: THEME.colors.statusGreen,
                fontSize: 11,
                fontWeight: 800,
                lineHeight: 1,
                padding: '5px 7px',
                borderRadius: 7,
                letterSpacing: 0,
                flexShrink: 0,
              }}
            >
              LIVE
            </span>
            <h2
              style={{
                margin: 0,
                color: THEME.colors.textBright,
                fontSize: 20,
                fontWeight: 650,
                lineHeight: 1.15,
                overflowWrap: 'anywhere',
              }}
            >
              Calling {phoneNumber}
            </h2>
            <span
              aria-label="Call elapsed time"
              style={{ color: THEME.colors.textMuted, fontSize: 13, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
            >
              {elapsedLabel}
            </span>
            {costLabel && (
              <span
                data-testid="phone-call-cost"
                aria-label="Estimated call cost"
                style={{
                  color: THEME.colors.textPrimary,
                  fontSize: 12,
                  fontWeight: 700,
                  fontVariantNumeric: 'tabular-nums',
                  padding: '4px 7px',
                  borderRadius: 8,
                  border: `1px solid ${THEME.colors.borderSubtle}`,
                  backgroundColor: THEME.colors.glassBase,
                  flexShrink: 0,
                }}
              >
                {costLabel}
              </span>
            )}
            <span
              data-testid="phone-recipient-state"
              style={{
                color: recipientState === 'hold' ? THEME.colors.statusYellow : THEME.colors.textMuted,
                fontSize: 12,
                fontWeight: 700,
                padding: '4px 7px',
                borderRadius: 8,
                border: `1px solid ${THEME.colors.borderSubtle}`,
                backgroundColor: recipientState === 'hold' ? `${THEME.colors.statusYellow}14` : THEME.colors.glassBase,
                flexShrink: 0,
              }}
            >
              {recipientLabel}
            </span>
            <button
              type="button"
              onClick={onOpenHistory}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                padding: '4px 7px',
                borderRadius: 8,
                border: `1px solid ${THEME.colors.borderSubtle}`,
                backgroundColor: THEME.colors.glassBase,
                color: THEME.colors.textMuted,
                fontSize: 12,
                fontWeight: 650,
                cursor: 'pointer',
              }}
            >
              <HistoryIcon />
              History
            </button>
          </div>
          <div style={{ minHeight: 18, marginTop: 4, color: THEME.colors.textMuted, fontSize: 13, lineHeight: 1.35 }}>
            {taskText || 'Active phone call'}
          </div>
        </div>
      </header>

      {queuedCalls.length > 0 && (
        <div
          data-testid="phone-call-queue"
          style={{
            marginBottom: 10,
            padding: '9px 10px',
            borderRadius: 8,
            border: `1px solid ${THEME.colors.borderSubtle}`,
            backgroundColor: THEME.colors.bgCard,
            flexShrink: 0,
          }}
        >
          <div style={{ color: THEME.colors.textMuted, fontSize: 11, fontWeight: 800, marginBottom: 6 }}>
            QUEUED
          </div>
          <div style={{ display: 'grid', gap: 6 }}>
            {queuedCalls.map((item, index) => {
              const position = Number(item.position) || index + 1;
              const queuedPhone = item.phone_number || item.phone || item.to || 'phone call';
              const queuedTask = formatTask(item.task) || 'Phone call';
              return (
                <div
                  key={item.queue_id || `${position}-${queuedPhone}`}
                  style={{
                    display: 'grid',
                    gridTemplateColumns: '28px minmax(0, 1fr) 28px',
                    alignItems: 'center',
                    gap: 8,
                    minHeight: 28,
                    color: THEME.colors.textPrimary,
                    fontSize: 12,
                  }}
                >
                  <span style={{ color: THEME.colors.textMuted, fontVariantNumeric: 'tabular-nums' }}>
                    {position}
                  </span>
                  <div
                    title={`${queuedPhone} - ${queuedTask}`}
                    style={{
                      minWidth: 0,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    <strong style={{ color: THEME.colors.textBright, fontWeight: 700 }}>{queuedPhone}</strong>
                    <span style={{ color: THEME.colors.textMuted }}> - {queuedTask}</span>
                  </div>
                  <button
                    type="button"
                    aria-label={`Remove queued call ${position}`}
                    onClick={() => onRemoveQueuedCall(position)}
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: 7,
                      border: `1px solid ${THEME.colors.borderSubtle}`,
                      backgroundColor: THEME.colors.glassBase,
                      color: THEME.colors.textMuted,
                      fontSize: 13,
                      fontWeight: 800,
                      lineHeight: 1,
                      cursor: 'pointer',
                    }}
                  >
                    x
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div
        ref={paneRef}
        data-testid="phone-call-transcript"
        style={{
          flex: 1,
          minHeight: 60,
          overflowY: 'auto',
          padding: '12px 14px',
          borderRadius: 12,
          backgroundColor: THEME.colors.bgCard,
          border: `1px solid ${THEME.colors.borderSubtle}`,
          color: THEME.colors.textPrimary,
          fontSize: 13,
          lineHeight: 1.45,
        }}
      >
        {transcripts.length === 0 ? (
          <div style={{ color: THEME.colors.textMuted, fontStyle: 'italic' }}>Live transcript</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {transcripts.map((entry, index) => {
              const role = entry.role || 'system';
              const isViola = role === 'viola';
              const isSystem = role === 'system';
              const speaker = transcriptSpeakerLabel(role, recipientName);
              return (
                <div
                  key={`${entry.ts || 'transcript'}-${index}`}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    // Viola speaks on the user's behalf = "own side" -> right. The person
                    // being called ("them") -> left. Matches the standard own-messages-on-
                    // the-right chat convention (2026-07-02 UX fix).
                    alignItems: isSystem ? 'stretch' : isViola ? 'flex-end' : 'flex-start',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                      alignSelf: isSystem ? 'flex-start' : isViola ? 'flex-end' : 'flex-start',
                      marginBottom: 3,
                      color: THEME.colors.textMuted,
                      fontSize: 10,
                      fontWeight: 700,
                      letterSpacing: 0.4,
                      textTransform: 'uppercase',
                    }}
                  >
                    <span>{speaker}</span>
                    {entry.partial && (
                      <span
                        aria-label={`${speaker} is live`}
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: 4,
                          padding: '2px 6px',
                          borderRadius: 999,
                          color: THEME.colors.statusGreen,
                          backgroundColor: `${THEME.colors.statusGreen}14`,
                          fontSize: 9,
                          fontWeight: 800,
                        }}
                      >
                        <span
                          aria-hidden="true"
                          style={{
                            width: 5,
                            height: 5,
                            borderRadius: '50%',
                            backgroundColor: 'currentColor',
                            boxShadow: `0 0 0 3px ${THEME.colors.statusGreen}18`,
                            animation: 'recordingBadgePulse 1.2s ease-in-out infinite',
                          }}
                        />
                        Live
                      </span>
                    )}
                  </div>
                  <div
                    data-testid="phone-call-transcript-line"
                    data-role={role}
                    data-partial={entry.partial ? 'true' : 'false'}
                    style={transcriptBubbleStyle(role, entry.partial)}
                  >
                    {entry.text}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {activeConsultation && (
        <form
          data-testid="phone-call-consult-inline"
          onSubmit={handleConsultSubmit}
          style={{
            marginTop: 10,
            padding: 10,
            borderRadius: 8,
            border: `1px solid ${THEME.colors.statusYellow}55`,
            backgroundColor: `${THEME.colors.statusYellow}12`,
            flexShrink: 0,
          }}
        >
          <div style={{ color: THEME.colors.statusYellow, fontSize: 12, fontWeight: 700, marginBottom: 6 }}>
            Viola needs your input
          </div>
          <div style={{ color: THEME.colors.textPrimary, fontSize: 13, lineHeight: 1.4, marginBottom: 10 }}>
            {activeConsultation.question}
          </div>
          <div style={{ display: 'flex', gap: 8, minWidth: 0 }}>
            <input
              type="text"
              value={consultAnswer}
              onChange={(event) => setConsultAnswer(event.target.value)}
              placeholder="Reply to the call agent..."
              data-testid="phone-call-consult-input"
              style={{
                minWidth: 0,
                flex: 1,
                padding: '9px 11px',
                borderRadius: 8,
                border: `1px solid ${THEME.colors.borderSubtle}`,
                backgroundColor: THEME.colors.bgCard,
                color: THEME.colors.textPrimary,
                fontSize: 13,
              }}
            />
            <button
              type="submit"
              disabled={!consultAnswer.trim()}
              style={{
                flexShrink: 0,
                padding: '9px 13px',
                borderRadius: 8,
                border: 'none',
                backgroundColor: consultAnswer.trim() ? THEME.colors.statusYellow : THEME.colors.borderSubtle,
                color: consultAnswer.trim() ? THEME.colors.bgVoid : THEME.colors.textMuted,
                fontSize: 13,
                fontWeight: 700,
                cursor: consultAnswer.trim() ? 'pointer' : 'default',
              }}
            >
              Reply
            </button>
            <button
              type="button"
              onClick={() => onConsultationTakeover(activeConsultation.call_id || callId)}
              style={{
                flexShrink: 0,
                padding: '9px 13px',
                borderRadius: 8,
                border: `1px solid ${THEME.colors.borderLight}`,
                backgroundColor: THEME.colors.glassBase,
                color: THEME.colors.textPrimary,
                fontSize: 13,
                fontWeight: 700,
                cursor: 'pointer',
              }}
            >
              Take over
            </button>
          </div>
        </form>
      )}

      <form
        onSubmit={handleOperatorSubmit}
        style={{
          display: 'flex',
          gap: 8,
          marginTop: 10,
          minWidth: 0,
          flexShrink: 0,
        }}
      >
        <textarea
          value={operatorText}
          onChange={(event) => setOperatorText(event.target.value)}
          onKeyDown={handleOperatorKeyDown}
          placeholder="Tell the call agent..."
          maxLength={1000}
          rows={1}
          data-testid="phone-call-operator-input"
          style={{
            minWidth: 0,
            flex: 1,
            resize: 'vertical',
            minHeight: 38,
            maxHeight: 120,
            padding: '10px 12px',
            borderRadius: 8,
            border: `1px solid ${THEME.colors.borderSubtle}`,
            backgroundColor: THEME.colors.bgCard,
            color: THEME.colors.textPrimary,
            fontSize: 13,
            outline: 'none',
          }}
        />
        <button
          type="submit"
          style={{
            flexShrink: 0,
            padding: '10px 14px',
            borderRadius: 8,
            border: 'none',
            backgroundColor: THEME.colors.accent,
            color: '#fff',
            fontSize: 13,
            fontWeight: 650,
            cursor: operatorText.trim() ? 'pointer' : 'default',
            opacity: operatorText.trim() ? 1 : 0.55,
          }}
        >
          Send
        </button>
      </form>

      <div
        data-testid="phone-suggested-actions"
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 6,
          marginTop: 8,
          minWidth: 0,
          flexShrink: 0,
        }}
      >
        {SUGGESTED_ACTIONS.map((message) => (
          <button
            key={message}
            type="button"
            onClick={() => handleSuggestedAction(message)}
            style={{
              padding: '6px 8px',
              borderRadius: 8,
              border: `1px solid ${THEME.colors.borderSubtle}`,
              backgroundColor: THEME.colors.bgCard,
              color: THEME.colors.textMuted,
              fontSize: 12,
              fontWeight: 650,
              lineHeight: 1.15,
              cursor: 'pointer',
              whiteSpace: 'normal',
            }}
          >
            {message}
          </button>
        ))}
      </div>

      <footer style={{ display: 'flex', gap: 10, marginTop: 10, flexWrap: 'wrap', flexShrink: 0 }}>
        <button
          type="button"
          onClick={onToggleListen}
          style={{
            padding: '10px 14px',
            borderRadius: 8,
            border: 'none',
            backgroundColor: isListening ? THEME.colors.statusGreen : THEME.colors.accent,
            color: '#fff',
            fontSize: 13,
            fontWeight: 650,
            cursor: 'pointer',
          }}
        >
          {isListening ? 'Stop listening' : 'Listen'}
        </button>

        <button
          type="button"
          onClick={takeoverActive ? onToggleTakeover : onActivateTakeover}
          disabled={takeoverPending}
          style={{
            padding: '10px 14px',
            borderRadius: 8,
            border: `1px solid ${takeoverActive ? THEME.colors.statusYellow : THEME.colors.borderLight}`,
            backgroundColor: takeoverActive ? `${THEME.colors.statusYellow}33` : THEME.colors.glassBase,
            color: takeoverActive ? THEME.colors.statusYellow : THEME.colors.textPrimary,
            fontSize: 13,
            fontWeight: 650,
            cursor: takeoverPending ? 'default' : 'pointer',
            opacity: takeoverPending ? 0.7 : 1,
          }}
        >
          {takeoverPending ? 'Calling owner' : takeoverActive ? 'Release' : 'Take over'}
        </button>

        <button
          type="button"
          onClick={onEndCall}
          style={{
            marginLeft: 'auto',
            padding: '10px 18px',
            borderRadius: 8,
            border: 'none',
            backgroundColor: THEME.colors.statusRed,
            color: '#fff',
            fontSize: 13,
            fontWeight: 700,
            cursor: 'pointer',
            boxShadow: `0 8px 20px ${THEME.colors.statusRed}33`,
          }}
        >
          End call
        </button>
      </footer>

      {recordingActive && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginTop: 8,
            color: THEME.colors.textMuted,
            fontSize: 12,
            flexShrink: 0,
          }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              backgroundColor: THEME.colors.statusRed,
              boxShadow: `0 0 0 4px ${THEME.colors.statusRed}18`,
              animation: 'recordingBadgePulse 1.2s ease-in-out infinite',
            }}
          />
          Recording
        </div>
      )}
    </section>
  );
}

PhoneCallPanel.propTypes = {
  callId: PropTypes.string.isRequired,
  callMeta: PropTypes.shape({
    phone_number: PropTypes.string,
    phone: PropTypes.string,
    to: PropTypes.string,
    recipient_name: PropTypes.string,
    business_name: PropTypes.string,
    current_cost_usd: PropTypes.number,
    cost_usd: PropTypes.number,
    estimated_cost_usd: PropTypes.number,
    recipient_state: PropTypes.string,
    started_at: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
    task: PropTypes.oneOfType([
      PropTypes.string,
      PropTypes.shape({
        description: PropTypes.string,
        title: PropTypes.string,
        summary: PropTypes.string,
      }),
    ]),
  }),
  onEndCall: PropTypes.func.isRequired,
  isListening: PropTypes.bool.isRequired,
  transcripts: PropTypes.arrayOf(PropTypes.shape({
    role: PropTypes.oneOf(['them', 'viola', 'system']),
    text: PropTypes.string.isRequired,
    partial: PropTypes.bool,
    ts: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
  })).isRequired,
  takeoverActive: PropTypes.bool.isRequired,
  takeoverPending: PropTypes.bool,
  onToggleListen: PropTypes.func.isRequired,
  onToggleTakeover: PropTypes.func.isRequired,
  onActivateTakeover: PropTypes.func.isRequired,
  onSendOperatorMessage: PropTypes.func.isRequired,
  onOpenHistory: PropTypes.func,
  activeConsultation: PropTypes.shape({
    call_id: PropTypes.string,
    question: PropTypes.string,
    urgency: PropTypes.string,
  }),
  onConsultationReply: PropTypes.func,
  onConsultationTakeover: PropTypes.func,
  queuedCalls: PropTypes.arrayOf(PropTypes.shape({
    queue_id: PropTypes.string,
    position: PropTypes.number,
    phone_number: PropTypes.string,
    phone: PropTypes.string,
    to: PropTypes.string,
    task: PropTypes.oneOfType([PropTypes.string, PropTypes.object]),
  })),
  onRemoveQueuedCall: PropTypes.func,
  recordingActive: PropTypes.bool,
};
