import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from './useViolaApi';

const BASE = '/v1/billing/capacity';
const TERMINAL = new Set(['applied', 'scheduled', 'succeeded', 'failed', 'expired', 'canceled']);
export const BILLING_ENTITLEMENT_EVENT = 'viola:billing-entitlement-changed';

export function billingPaymentUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password
      && ['checkout.stripe.com', 'invoice.stripe.com', 'billing.stripe.com'].includes(url.hostname)
      ? url.href : '';
  } catch { return ''; }
}

export function billingOperationStatus(operation) {
  return operation?.status || operation?.state || '';
}

async function request(path, options) {
  const result = await apiFetch(`${BASE}${path}`, options);
  if (!result || result.ok === false) throw new Error('Billing is temporarily unavailable. Please retry.');
  return result;
}

function savedAttempt(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null');
    return value && typeof value.idempotency_key === 'string' && value.request
      ? value : null;
  } catch { return null; }
}

// Only opaque operation/quote IDs and the customer's consent request are saved.
// The backend owns account identity, prices, payment and entitlement application.
export function useBillingCapacity({ accountId, refreshUser }) {
  const storageKey = accountId ? `viola:billing-capacity:${accountId}` : '';
  const [catalog, setCatalog] = useState(null);
  const [account, setAccount] = useState(null);
  const [quote, setQuote] = useState(null);
  const [attempt, setAttempt] = useState(() => savedAttempt(storageKey));
  const [operation, setOperation] = useState(null);
  const [loading, setLoading] = useState(true);
  const [quoting, setQuoting] = useState(false);
  const [changing, setChanging] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(false);
  const quoteEpoch = useRef(0);
  const mutation = useRef(false);
  const recovery = useRef(false);
  const latestAttempt = useRef(attempt);
  const refreshedOperation = useRef('');
  const refreshUserRef = useRef(refreshUser);
  refreshUserRef.current = refreshUser;

  const saveAttempt = useCallback((value) => {
    latestAttempt.current = value;
    setAttempt(value);
    if (!storageKey) return;
    try {
      if (value) localStorage.setItem(storageKey, JSON.stringify(value));
      else localStorage.removeItem(storageKey);
    } catch { /* recovery also remains in memory when browser storage is unavailable */ }
  }, [storageKey]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextCatalog, nextAccount] = await Promise.all([request('/catalog'), request('')]);
      if (!mounted.current) return;
      setCatalog(nextCatalog);
      setAccount(nextAccount);
      const pendingId = nextAccount.pending_change?.operation_id || nextAccount.pending_change?.id;
      if (pendingId && !latestAttempt.current) {
        saveAttempt({ operation_id: pendingId, idempotency_key: '', request: {} });
      }
      setError('');
    } catch (failure) {
      if (mounted.current) setError(failure.message || 'Billing could not be loaded.');
    } finally { if (mounted.current) setLoading(false); }
  }, [saveAttempt]);

  const acceptOperation = useCallback(async (next, currentAttempt) => {
    if (!mounted.current) return;
    const id = next.operation_id || next.id;
    if (!id) throw new Error('The change status is unavailable. Retry the same confirmation to recover it.');
    setOperation(next);
    const status = billingOperationStatus(next);
    if (TERMINAL.has(status)) {
      saveAttempt(null);
      setQuote(null);
      if (['applied', 'scheduled', 'succeeded'].includes(status)) {
        await load();
        if (status !== 'scheduled' && refreshedOperation.current !== id) {
          refreshedOperation.current = id;
          // Refresh the existing session; this never replays the user's task.
          try { await refreshUserRef.current?.(); } catch { /* capacity read remains authoritative */ }
          window.dispatchEvent(new Event(BILLING_ENTITLEMENT_EVENT));
        }
      }
    } else saveAttempt({ ...currentAttempt, operation_id: id });
  }, [load, saveAttempt]);

  const recover = useCallback(async () => {
    const current = latestAttempt.current;
    if (!current?.operation_id || recovery.current) return;
    recovery.current = true;
    try {
      const next = await request(`/changes/${encodeURIComponent(current.operation_id)}`);
      if (!mounted.current) return;
      await acceptOperation(next, current);
      setError('');
    } catch (failure) {
      if (mounted.current) setError(failure.message || 'Payment status could not be checked.');
    } finally { recovery.current = false; }
  }, [acceptOperation]);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => { mounted.current = false; quoteEpoch.current += 1; };
  }, [load]);

  const operationId = attempt?.operation_id;
  const needsReview = billingOperationStatus(operation) === 'remediation_required';
  useEffect(() => {
    if (!operationId || needsReview) return undefined;
    let canceled = false;
    let timer;
    let checks = 0;
    const check = async () => {
      if (canceled) return;
      await recover();
      checks += 1;
      if (!canceled && checks < 8 && latestAttempt.current?.operation_id === operationId) {
        timer = setTimeout(check, Math.min(1500 * (2 ** checks), 15000));
      }
    };
    check();
    return () => { canceled = true; clearTimeout(timer); };
  }, [operationId, needsReview, recover]);

  useEffect(() => {
    const onReturn = () => {
      if (document.visibilityState !== 'hidden') recover();
    };
    window.addEventListener('focus', onReturn);
    document.addEventListener('visibilitychange', onReturn);
    return () => {
      window.removeEventListener('focus', onReturn);
      document.removeEventListener('visibilitychange', onReturn);
    };
  }, [recover]);

  const invalidateQuote = useCallback(() => {
    quoteEpoch.current += 1;
    setQuote(null);
    setQuoting(false);
    setError('');
  }, []);

  const preview = useCallback(async (selection) => {
    const epoch = ++quoteEpoch.current;
    setQuote(null);
    setQuoting(true);
    setError('');
    try {
      const next = await request('/quote', { method: 'POST', body: JSON.stringify(selection) });
      if (mounted.current && epoch === quoteEpoch.current) setQuote(next);
    } catch (failure) {
      if (mounted.current && epoch === quoteEpoch.current) setError(failure.message);
    } finally {
      if (mounted.current && epoch === quoteEpoch.current) setQuoting(false);
    }
  }, []);

  const submit = useCallback(async (requestBody, path = '/change') => {
    if (mutation.current) return;
    mutation.current = true;
    setChanging(true);
    setError('');
    const existing = latestAttempt.current;
    const current = existing || {
      idempotency_key: crypto.randomUUID(),
      request: requestBody,
      path,
    };
    saveAttempt(current);
    try {
      const next = await request(current.path || '/change', {
        method: 'POST',
        body: JSON.stringify({ ...current.request, idempotency_key: current.idempotency_key }),
      });
      if (!mounted.current) return;
      if (current.path === '/scheduled/cancel' && billingOperationStatus(next) === 'canceled') {
        saveAttempt(null);
        setOperation({ status: 'schedule_canceled' });
        await load();
        return;
      }
      await acceptOperation(next, current);
      const url = billingPaymentUrl(next.authentication_url || next.checkout_url);
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
    } catch (failure) {
      if (mounted.current) {
        if ([400, 403, 409, 410, 422].includes(failure.status)) {
          saveAttempt(null);
          setQuote(null);
          setError('This quote can no longer be confirmed. Review a fresh quote before continuing.');
        } else setError('The result could not be confirmed. Recover the same change before starting another.');
      }
    } finally {
      mutation.current = false;
      if (mounted.current) setChanging(false);
    }
  }, [acceptOperation, load, saveAttempt]);

  const retry = useCallback(() => {
    const current = latestAttempt.current;
    return current?.operation_id ? recover() : current ? submit(current.request, current.path) : load();
  }, [load, recover, submit]);

  return { catalog, account, quote, attempt, operation, loading, quoting, changing, error,
    load, preview, invalidateQuote, submit, retry };
}
