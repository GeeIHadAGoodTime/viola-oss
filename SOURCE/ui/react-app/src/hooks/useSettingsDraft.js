import { useCallback, useLayoutEffect, useRef, useState } from 'react';

const owns = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const copy = value => Array.isArray(value)
  ? value.map(copy)
  : value && typeof value === 'object'
    ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, copy(item)]))
    : value;
const equal = (left, right) => {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => owns(right, key) && equal(left[key], right[key]));
};
const sameField = (left, right, key) => owns(left, key) === owns(right, key) && equal(left[key], right[key]);
const fields = (...values) => new Set(values.flatMap(value => Object.keys(value)));
const retainField = (next, draft, key) => {
  if (owns(draft, key)) {
    Object.defineProperty(next, key, { value: copy(draft[key]), enumerable: true, configurable: true, writable: true });
  } else {
    delete next[key];
  }
};

// Local drafts have different ownership from server settings. Snapshot refreshes
// update clean fields; a save receipt only owns fields not edited since submit.
export function useSettingsDraft(serverSettings, isOpen) {
  const latestServer = useRef(serverSettings);
  latestServer.current = serverSettings;
  const sessionRef = useRef(null);
  const [view, setView] = useState({ draft: copy(serverSettings || {}), hasChanges: false, ackVersion: 0 });
  const publish = useCallback(session => {
    if (sessionRef.current !== session) return;
    setView({ draft: session.draft, hasChanges: !equal(session.draft, session.server), ackVersion: session.ackVersion });
  }, []);

  useLayoutEffect(() => {
    const server = copy(latestServer.current || {});
    const session = isOpen ? { server, draft: copy(server), edits: {}, nextEdit: 0, pending: new Set(), ackVersion: 0 } : null;
    sessionRef.current = session;
    if (session) publish(session);
    return () => {
      if (sessionRef.current === session) sessionRef.current = null;
    };
  }, [isOpen, publish]);

  useLayoutEffect(() => {
    const session = sessionRef.current;
    if (!session || !isOpen) return;
    const server = copy(serverSettings || {});
    const next = copy(server);
    for (const key of fields(session.draft, session.server, session.edits)) {
      const editedWhileSaving = [...session.pending].some(save => (session.edits[key] || 0) > (save.edits[key] || 0));
      if (!sameField(session.draft, session.server, key) || editedWhileSaving) retainField(next, session.draft, key);
    }
    session.server = server;
    session.draft = next;
    publish(session);
  }, [serverSettings, isOpen, view.ackVersion, publish]);

  const setDraft = useCallback(update => {
    const session = sessionRef.current;
    if (!session) return;
    const next = copy(typeof update === 'function' ? update(copy(session.draft)) : update);
    for (const key of fields(session.draft, next)) {
      if (!sameField(session.draft, next, key)) session.edits[key] = ++session.nextEdit;
    }
    session.draft = next;
    publish(session);
  }, [publish]);

  const beginSave = useCallback((submitted) => {
    const session = sessionRef.current;
    if (!session) return null;
    const ticket = { session, submitted: copy(submitted ?? session.draft), edits: { ...session.edits } };
    session.pending.add(ticket);
    return ticket;
  }, []);

  const finishSave = useCallback((ticket, receipt, adopt = true) => {
    const session = sessionRef.current;
    if (!session || ticket?.session !== session || !session.pending.has(ticket)) return { applied: false };
    session.pending.delete(ticket);
    const laterFields = [...fields(session.edits, ticket.edits)].filter(key => (session.edits[key] || 0) > (ticket.edits[key] || 0));
    const accepted = receipt?.ok === true;
    if (adopt && accepted) {
      const acknowledged = copy(receipt.settings || {});
      const next = copy(acknowledged);
      for (const key of laterFields) retainField(next, session.draft, key);
      session.server = acknowledged;
      session.draft = next;
      // Reconcile once more against the current hook snapshot. It may already
      // be newer than this particular receipt; Cancel always uses that source.
      session.ackVersion += 1;
    }
    publish(session);
    return { applied: adopt, accepted, laterEdits: laterFields.length > 0 };
  }, [publish]);

  const resetDraft = useCallback(() => {
    const session = sessionRef.current;
    if (!session) return;
    session.server = copy(latestServer.current || {});
    session.draft = copy(session.server);
    session.edits = {};
    session.pending.clear();
    publish(session);
  }, [publish]);

  return { draft: view.draft, hasChanges: view.hasChanges, setDraft, beginSave, finishSave, resetDraft };
}
