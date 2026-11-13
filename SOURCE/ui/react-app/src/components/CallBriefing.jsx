/**
 * CallBriefing — Viola's one confirm-or-cancel card.
 *
 * Every action that stops at a confirm gate shows here: a phone call with its
 * exact number, and anything else (opening an app, a shell command, a calendar
 * change, a delete) with the same words Viola says out loud. Payments are the
 * exception: they have their own secure review.
 *
 * A dial stops at the agent's unified confirmation gate (phone calls ask once
 * per task; a number found online, or any number not yet confirmed while web
 * content is in the conversation, is asked about again). The executor
 * broadcasts that gate as a `consult_user` event with action_class
 * `phone_call`; `callBriefingFromConsultEvent` turns it into this card.
 *
 * The card always shows the exact number next to who it belongs to. Approve and
 * Cancel answer the same gate the user can answer by voice or by typing: they
 * send "confirm" or "cancel" through the normal command path, which resolves
 * the pending confirmation (the dial happens only after a yes). A call asked
 * for in a saved chat is pending in that chat only, so the answer goes to that
 * chat (`answerCallBriefing`).
 */
import React, { useEffect, useLayoutEffect, useState } from 'react';
import { THEME } from '../config';

const theme = THEME;

const AWAITING_STATUSES = new Set(['pending', 'deferred']);
const CLEARED_STATUSES = new Set(['denied', 'unavailable']);

export const CALL_APPROVE_COMMAND = 'confirm';
export const CALL_CANCEL_COMMAND = 'cancel';
// Matches the backend's pending-confirmation lifetime: after this nothing can
// answer the card, so it must not stay on screen.
export const ACTION_APPROVAL_TTL_MS = 300000;

const OWN_CARD_ACTION_CLASSES = new Set(['phone_call', 'payment']);

/**
 * Map a `consult_user` WebSocket payload to call-briefing state.
 *
 * Returns `undefined` for events that are not phone-call confirmations (leave
 * the card alone), `null` to clear the card, or the briefing object to show.
 */
export function callBriefingFromConsultEvent(payload) {
  if (!payload || typeof payload !== 'object') return undefined;
  if (payload.action_class !== 'phone_call') return undefined;
  const status = String(payload.status || '').toLowerCase();
  if (CLEARED_STATUSES.has(status)) return null;
  const awaiting = AWAITING_STATUSES.has(status);
  if (!awaiting && status !== 'approved') return undefined;
  return {
    phone_number: payload.phone_number || '',
    business_name: payload.recipient_name || '',
    objective: payload.objective || '',
    planned_action: payload.planned_action || payload.message || '',
    untrusted_source: Boolean(payload.untrusted_source),
    task_id: payload.task_id || null,
    chat_thread_id: payload.chat_thread_id || null,
    awaiting_confirmation: awaiting,
    approved: status === 'approved',
  };
}

/**
 * Map a `consult_user` payload for any action other than a call to card state.
 *
 * Same contract as `callBriefingFromConsultEvent`: `undefined` leaves the card
 * alone, `null` clears it (answered, refused, expired), an object shows it.
 */
export function actionApprovalFromConsultEvent(payload) {
  if (!payload || typeof payload !== 'object') return undefined;
  if (!payload.action_class || OWN_CARD_ACTION_CLASSES.has(payload.action_class)) return undefined;
  const status = String(payload.status || '').toLowerCase();
  if (!AWAITING_STATUSES.has(status)) return null;
  return {
    planned_action: payload.planned_action || payload.message || '',
    action_class: payload.action_class,
    task_id: payload.task_id || null,
    chat_thread_id: payload.chat_thread_id || null,
    awaiting_confirmation: true,
    approved: false,
  };
}

/**
 * Send the card's "confirm" / "cancel" to the conversation that asked.
 *
 * A pending confirmation belongs to the chat it was asked in and no other chat
 * can answer it, so a call requested in a saved chat is answered in that chat's
 * thread; everything else goes through the normal command path.
 */
export function answerCallBriefing(briefing, text, { sendCommand, sendToChat }) {
  const threadId = briefing && briefing.chat_thread_id;
  if (threadId && typeof sendToChat === 'function') return sendToChat(threadId, text);
  return sendCommand(text);
}

const buttonBase = {
  flex: 1,
  borderRadius: 8,
  padding: '8px 12px',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
};

/**
 * True when a different conversation is on screen from the one this approval
 * belongs to. The approval waits where it was asked: only that conversation
 * can answer it, so showing the card elsewhere invites a "yes" that can't count.
 *
 * An approval with no chat id was asked in the main conversation (by voice or
 * on the stage). It used to show over every saved chat as well (1.0.5 build
 * 10, bug 10); now it waits until the user is back outside a saved chat.
 */
export function callBriefingBelongsElsewhere(briefing, openChatThreadId) {
  if (!briefing || !openChatThreadId) return false;
  return (briefing.chat_thread_id || null) !== openChatThreadId;
}

const CARD_EDGE_GAP_PX = 20;
const CARD_COMPOSER_GAP_PX = 12;

/**
 * How far from the bottom of the window the card sits. Normally the edge gap;
 * when the chat composer is on screen, just above it, so the card never
 * covers the chat's Send button (1.0.5 build 10, bug 10).
 */
export function cardBottomOffset(composerRect, viewportHeight) {
  if (!composerRect || !(composerRect.height > 0) || !(viewportHeight > 0)) return CARD_EDGE_GAP_PX;
  if (composerRect.top >= viewportHeight || composerRect.bottom <= 0) return CARD_EDGE_GAP_PX;
  return Math.max(CARD_EDGE_GAP_PX, Math.round(viewportHeight - composerRect.top + CARD_COMPOSER_GAP_PX));
}

function useBottomClearOfComposer(active) {
  const [bottom, setBottom] = useState(CARD_EDGE_GAP_PX);
  useLayoutEffect(() => {
    if (!active) return undefined;
    const measure = () => {
      const composer = document.querySelector('.chat-composer-row');
      setBottom(cardBottomOffset(composer ? composer.getBoundingClientRect() : null, window.innerHeight));
    };
    measure();
    // The composer grows with a long draft and comes and goes with the chat.
    const timer = setInterval(measure, 400);
    window.addEventListener('resize', measure);
    return () => {
      clearInterval(timer);
      window.removeEventListener('resize', measure);
    };
  }, [active]);
  return bottom;
}

export default function CallBriefing({ briefing, onDismiss, onApprove, onCancel }) {
  const [sending, setSending] = useState(null);

  // A new confirmation (or its resolution) re-enables the buttons.
  useEffect(() => {
    setSending(null);
  }, [briefing?.phone_number, briefing?.task_id, briefing?.awaiting_confirmation, briefing?.approved]);
  const bottom = useBottomClearOfComposer(Boolean(briefing));

  if (!briefing) return null;

  const {
    phone_number,
    business_name,
    objective,
    talking_points,
    approved,
    awaiting_confirmation,
    untrusted_source,
    planned_action,
  } = briefing;
  const isCall = !briefing.action_class;
  const canAnswer = Boolean(awaiting_confirmation && !approved && (onApprove || onCancel));
  const accent = approved ? theme.colors.statusGreen : theme.colors.statusYellow;
  let title = 'Call plan';
  if (approved) title = 'Call approved';
  else if (awaiting_confirmation) title = 'Viola needs your approval';

  const answer = async (kind) => {
    if (sending) return;
    const handler = kind === 'approve' ? onApprove : onCancel;
    if (!handler) return;
    setSending(kind);
    try {
      await handler(briefing);
    } catch {
      // The command path surfaces its own error; let the user try again.
      setSending(null);
    }
  };

  return (
    <div
      role="dialog"
      aria-label={isCall ? 'Phone call approval' : 'Action approval'}
      style={{
        position: 'fixed',
        bottom,
        right: CARD_EDGE_GAP_PX,
        width: 380,
        maxWidth: 'calc(100vw - 40px)',
        background: '#1a1a2e',
        border: `1px solid ${accent}`,
        borderRadius: 12,
        padding: 16,
        zIndex: 9998,
        boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: accent, fontWeight: 600, fontSize: 13 }}>{title}</span>
        <button
          type="button"
          className="viola-touch-target"
          aria-label="Dismiss"
          onClick={onDismiss}
          style={{
            background: 'none',
            border: 'none',
            color: '#888',
            cursor: 'pointer',
            fontSize: 16,
            lineHeight: 1,
            padding: '6px 8px',
            margin: '-6px -8px -6px 0',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          x
        </button>
      </div>

      {!isCall ? (
        <div data-testid="action-approval-text" style={{ color: '#eee', fontSize: 13, marginBottom: 4 }}>
          Viola wants to {planned_action}.
        </div>
      ) : (
      <div style={{ color: '#ccc', fontSize: 13, marginBottom: 4 }}>
        Call:{' '}
        {business_name ? (
          <>
            <strong style={{ color: '#eee' }}>{business_name}</strong>
            {phone_number ? (
              <>
                {' at '}
                <strong data-testid="call-briefing-number" style={{ color: '#eee' }}>{phone_number}</strong>
              </>
            ) : null}
          </>
        ) : (
          <strong data-testid="call-briefing-number" style={{ color: '#eee' }}>
            {phone_number || planned_action}
          </strong>
        )}
      </div>
      )}

      {untrusted_source && (
        <div style={{ color: theme.colors.statusYellow, fontSize: 12, marginBottom: 6 }}>
          Viola got this number from a search result or other outside content. Check it is the right one before you approve.
        </div>
      )}

      {objective ? (
        <div style={{ color: '#ccc', fontSize: 13, marginBottom: 8 }}>
          What Viola will do: {objective}
        </div>
      ) : null}

      {talking_points && talking_points.length > 0 && (
        <ul style={{ color: '#aaa', fontSize: 12, paddingLeft: 20, margin: '4px 0' }}>
          {talking_points.map((pt, i) => (
            <li key={i} style={{ marginBottom: 2 }}>
              {pt}
            </li>
          ))}
        </ul>
      )}

      {canAnswer && (
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <button
            type="button"
            className="viola-touch-target"
            onClick={() => answer('approve')}
            disabled={Boolean(sending)}
            style={{
              ...buttonBase,
              background: theme.colors.statusGreen,
              border: 'none',
              color: '#0b0b14',
              opacity: sending ? 0.6 : 1,
            }}
          >
            {sending === 'approve' ? 'Calling...' : 'Approve'}
          </button>
          <button
            type="button"
            className="viola-touch-target"
            onClick={() => answer('cancel')}
            disabled={Boolean(sending)}
            style={{
              ...buttonBase,
              background: 'none',
              border: '1px solid #555',
              color: '#ddd',
              opacity: sending ? 0.6 : 1,
            }}
          >
            {sending === 'cancel' ? 'Cancelling...' : 'Cancel'}
          </button>
        </div>
      )}
    </div>
  );
}
