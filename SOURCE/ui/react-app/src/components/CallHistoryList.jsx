/* eslint react/jsx-uses-vars: "error" */
import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { fetchCallHistory, getCallTranscript } from '../hooks/useCallAudio';
import { toUserMessage } from '../utils/userFacingError';
import CallHistoryRow from './CallHistoryRow';

export default function CallHistoryList({ pageSize = 50, focusCallId = null }) {
  const [calls, setCalls] = useState([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [expandedCallId, setExpandedCallId] = useState(null);
  const [transcriptsByCall, setTranscriptsByCall] = useState({});
  const rowRefs = useRef({});

  const loadPage = useCallback(async (nextOffset = 0) => {
    const isFirstPage = nextOffset === 0;
    if (isFirstPage) setLoading(true);
    else setLoadingMore(true);
    setError('');

    try {
      const page = await fetchCallHistory(pageSize, nextOffset);
      const pageCalls = Array.isArray(page.calls) ? page.calls : [];
      setCalls((current) => (isFirstPage ? pageCalls : [...current, ...pageCalls]));
      setOffset(nextOffset + pageCalls.length);
      setHasMore(pageCalls.length === pageSize);
    } catch (err) {
      setError(toUserMessage(err, 'Could not load your call history. Please try again.'));
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [pageSize]);

  useEffect(() => {
    void loadPage(0);
  }, [loadPage]);

  const loadTranscriptForCall = useCallback(async (callId) => {
    if (transcriptsByCall[callId]?.data || transcriptsByCall[callId]?.loading) return;

    setTranscriptsByCall((current) => ({
      ...current,
      [callId]: { loading: true, error: '', data: null },
    }));
    try {
      const data = await getCallTranscript(callId);
      setTranscriptsByCall((current) => ({
        ...current,
        [callId]: { loading: false, error: '', data: data || { transcript: [] } },
      }));
    } catch (err) {
      setTranscriptsByCall((current) => ({
        ...current,
        [callId]: {
          loading: false,
          error: toUserMessage(err, 'Could not load the call transcript. Please try again.'),
          data: null,
        },
      }));
    }
  }, [transcriptsByCall]);

  const handleToggleRow = useCallback(async (call) => {
    const callId = call.call_id;
    if (expandedCallId === callId) {
      setExpandedCallId(null);
      return;
    }

    setExpandedCallId(callId);
    await loadTranscriptForCall(callId);
  }, [expandedCallId, loadTranscriptForCall]);

  useEffect(() => {
    if (!focusCallId || loading) return;
    const focusedCall = calls.find((call) => call.call_id === focusCallId);
    if (!focusedCall) return;

    setExpandedCallId(focusCallId);
    rowRefs.current[focusCallId]?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
    void loadTranscriptForCall(focusCallId);
  }, [calls, focusCallId, loadTranscriptForCall, loading]);

  return (
    <section
      aria-label="Phone call history"
      data-testid="call-history-list"
      style={{
        display: 'flex',
        flexDirection: 'column',
        flex: 1,
        minHeight: 0,
        marginTop: 'clamp(20px, 3vh, 32px)',
        marginBottom: 'clamp(16px, 2.5vh, 24px)',
        padding: 16,
        borderRadius: 12,
        backgroundColor: THEME.colors.bgElevated,
        border: `1px solid ${THEME.colors.borderLight}`,
        boxShadow: `0 18px 60px ${THEME.colors.shadowMedium}`,
        color: THEME.colors.textPrimary,
      }}
    >
      <header style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 14 }}>
        <div>
          <h2 style={{ margin: 0, color: THEME.colors.textBright, fontSize: 20, fontWeight: 650 }}>
            Call history
          </h2>
          <div style={{ minHeight: 18, marginTop: 4, color: THEME.colors.textMuted, fontSize: 13 }}>
            {calls.length ? `${calls.length} calls loaded` : 'Phone'}
          </div>
        </div>
      </header>

      {loading ? (
        <div style={{ color: THEME.colors.textMuted, padding: '18px 2px' }}>Loading calls...</div>
      ) : error ? (
        <div style={{ color: THEME.colors.statusRed, padding: '18px 2px' }}>{error}</div>
      ) : calls.length === 0 ? (
        <div
          data-testid="call-history-empty"
          style={{
            display: 'grid',
            placeItems: 'center',
            flex: 1,
            minHeight: 180,
            color: THEME.colors.textMuted,
            border: `1px dashed ${THEME.colors.borderSubtle}`,
            borderRadius: 8,
          }}
        >
          No call history yet.
        </div>
      ) : (
        <>
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
              overflowY: 'auto',
              minHeight: 0,
              paddingRight: 2,
              paddingBottom: 18,
              scrollPaddingBottom: 18,
            }}
          >
            {calls.map((call) => (
              <CallHistoryRow
                key={call.call_id}
                rowRef={(node) => {
                  if (node) rowRefs.current[call.call_id] = node;
                  else delete rowRefs.current[call.call_id];
                }}
                call={call}
                expanded={expandedCallId === call.call_id}
                transcriptState={transcriptsByCall[call.call_id]}
                onToggle={() => handleToggleRow(call)}
              />
            ))}
          </div>
          {hasMore && (
            <button
              type="button"
              onClick={() => loadPage(offset)}
              disabled={loadingMore}
              style={{
                alignSelf: 'center',
                marginTop: 14,
                padding: '10px 14px',
                borderRadius: 8,
                border: `1px solid ${THEME.colors.borderLight}`,
                backgroundColor: THEME.colors.glassBase,
                color: THEME.colors.textPrimary,
                fontSize: 13,
                fontWeight: 650,
                cursor: loadingMore ? 'default' : 'pointer',
                opacity: loadingMore ? 0.65 : 1,
              }}
            >
              {loadingMore ? 'Loading...' : 'Load more'}
            </button>
          )}
        </>
      )}
    </section>
  );
}

CallHistoryList.propTypes = {
  pageSize: PropTypes.number,
  focusCallId: PropTypes.string,
};
