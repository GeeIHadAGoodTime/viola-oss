import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useSettingsDraft } from './useSettingsDraft';

const base = { mic_muted: false, voice_mode: 'wake_word', tts_volume: 1 };
function open(server = base, options = {}) {
  const view = renderHook(props => useSettingsDraft(props.server, props.open), {
    initialProps: { server, open: true }, ...options,
  });
  view.push = next => act(() => view.rerender({ server: next, open: true }));
  view.edit = patch => act(() => view.result.current.setDraft(value => ({ ...value, ...patch })));
  view.accept = (ticket, acknowledged, currentServer = acknowledged) => {
    let result;
    act(() => {
      view.rerender({ server: currentServer, open: true });
      result = view.result.current.finishSave(ticket, { ok: true, settings: acknowledged });
    });
    return result;
  };
  return view;
}

describe('useSettingsDraft ownership', () => {
  it('adopts initial and later clean snapshots', () => {
    const view = open();
    expect(view.result.current.draft).toEqual(base);
    view.push({ ...base, tts_volume: 0.4 });
    expect(view.result.current.draft.tts_volume).toBe(0.4);
    expect(view.result.current.hasChanges).toBe(false);
  });

  it.each([{ mic_muted: true }, { voice_mode: 'disabled' }])('retains unsaved privacy fields %j while clean fields update', patch => {
    const view = open();
    view.edit(patch);
    view.push({ ...base, tts_volume: 0.42 });
    expect(view.result.current.draft).toEqual({ ...base, tts_volume: 0.42, ...patch });
    expect(view.result.current.hasChanges).toBe(true);
  });

  it('does not keep a field dirty when the server reaches the same value', () => {
    const view = open();
    view.edit({ mic_muted: true });
    view.push({ ...base, mic_muted: true });
    expect(view.result.current.hasChanges).toBe(false);
  });

  it('keeps a post-submit revert even though it equals the prior server value', () => {
    const view = open();
    view.edit({ mic_muted: true });
    const ticket = view.result.current.beginSave();
    view.edit({ mic_muted: false });
    view.push({ ...base, mic_muted: true });
    expect(view.result.current.draft.mic_muted).toBe(false);
    const result = view.accept(ticket, { ...base, mic_muted: true });
    expect(result.laterEdits).toBe(true);
    expect(view.result.current.draft.mic_muted).toBe(false);
    expect(view.result.current.hasChanges).toBe(true);
  });

  it('adopts normalized values and redacted secrets while retaining later edits', () => {
    const view = open({ ...base, llm_api_key: 'redacted' });
    view.edit({ llm_api_key: 'synthetic input', tts_volume: 2 });
    const ticket = view.result.current.beginSave();
    view.edit({ voice_mode: 'disabled' });
    const receipt = { ...base, llm_api_key: 'redacted', tts_volume: 1 };
    const result = view.accept(ticket, receipt);
    expect(result.laterEdits).toBe(true);
    expect(view.result.current.draft).toEqual({ ...receipt, voice_mode: 'disabled' });
    expect(view.result.current.hasChanges).toBe(true);
  });

  it('uses edit ownership rather than value equality for an ABA edit', () => {
    const view = open({ ...base, llm_api_key: 'redacted' });
    view.edit({ llm_api_key: 'first synthetic input' });
    const ticket = view.result.current.beginSave();
    view.edit({ llm_api_key: 'second synthetic input' });
    view.edit({ llm_api_key: 'first synthetic input' });
    view.accept(ticket, { ...base, llm_api_key: 'redacted' });
    expect(view.result.current.draft.llm_api_key).toBe('first synthetic input');
    expect(view.result.current.hasChanges).toBe(true);
  });

  it.each([false, true])('adopts receipt removals only for fields without later edits (%s)', laterEdit => {
    const view = open({ ...base, removed: 'old' });
    const ticket = view.result.current.beginSave();
    if (laterEdit) view.edit({ removed: 'new' });
    view.accept(ticket, base);
    expect(Object.hasOwn(view.result.current.draft, 'removed')).toBe(laterEdit);
    if (laterEdit) expect(view.result.current.draft.removed).toBe('new');
  });

  it('preserves a local field removal across a refresh', () => {
    const view = open({ ...base, removed: 'old' });
    act(() => view.result.current.setDraft(value => { delete value.removed; return value; }));
    view.push({ ...base, removed: 'server-new', tts_volume: 0.5 });
    expect(Object.hasOwn(view.result.current.draft, 'removed')).toBe(false);
    expect(view.result.current.draft.tts_volume).toBe(0.5);
  });

  it('adopts a snapshot newer than the receipt without reviving accepted dirty values', () => {
    const view = open();
    view.edit({ mic_muted: true });
    const ticket = view.result.current.beginSave();
    view.accept(ticket, { ...base, mic_muted: true }, { ...base, tts_volume: 0.25 });
    expect(view.result.current.draft).toEqual({ ...base, tts_volume: 0.25 });
    expect(view.result.current.hasChanges).toBe(false);
  });

  it('retains a draft after refusal and supports an acknowledged retry', () => {
    const view = open();
    view.edit({ mic_muted: true });
    let first = view.result.current.beginSave();
    act(() => view.result.current.finishSave(first, { ok: false }));
    expect(view.result.current.draft.mic_muted).toBe(true);
    expect(view.result.current.hasChanges).toBe(true);
    first = view.result.current.beginSave();
    expect(view.accept(first, { ...base, mic_muted: true }).laterEdits).toBe(false);
    expect(view.result.current.hasChanges).toBe(false);
  });

  it('retires an older receipt without applying it', () => {
    const view = open();
    view.edit({ mic_muted: true });
    const first = view.result.current.beginSave();
    view.edit({ voice_mode: 'disabled' });
    const second = view.result.current.beginSave();
    view.accept(second, { ...base, mic_muted: true, voice_mode: 'disabled' });
    act(() => view.result.current.finishSave(first, { ok: true, settings: base }, false));
    expect(view.result.current.draft.voice_mode).toBe('disabled');
  });

  it('Cancel uses the newest server snapshot and invalidates old tickets', () => {
    const view = open();
    view.edit({ voice_mode: 'disabled' });
    const ticket = view.result.current.beginSave();
    const newest = { ...base, tts_volume: 0.2 };
    view.push(newest);
    act(() => view.result.current.resetDraft());
    expect(view.result.current.draft).toEqual(newest);
    expect(view.result.current.hasChanges).toBe(false);
    let result;
    act(() => { result = view.result.current.finishSave(ticket, { ok: true, settings: { ...base, voice_mode: 'disabled' } }); });
    expect(result.applied).toBe(false);
    expect(view.result.current.draft).toEqual(newest);
  });

  it('does not reuse a ticket after close and reopen', () => {
    const view = open();
    view.edit({ mic_muted: true });
    const ticket = view.result.current.beginSave();
    act(() => view.rerender({ server: base, open: false }));
    expect(view.result.current.beginSave()).toBeNull();
    view.push({ ...base, tts_volume: 0.3 });
    let result;
    act(() => { result = view.result.current.finishSave(ticket, { ok: true, settings: { ...base, mic_muted: true } }); });
    expect(result.applied).toBe(false);
    expect(view.result.current.draft.mic_muted).toBe(false);
  });

  it('does not mutate the caller snapshot or treat nested property order as an edit', () => {
    const server = { nested: { a: 1, b: [2, 3] } };
    const view = open(server);
    view.edit({ nested: { b: [2, 3], a: 1 } });
    expect(view.result.current.hasChanges).toBe(false);
    act(() => view.result.current.setDraft(value => { value.nested.b.push(4); return value; }));
    expect(server.nested.b).toEqual([2, 3]);
    expect(view.result.current.hasChanges).toBe(true);
  });

  it('keeps the active lifetime functional after StrictMode replay', () => {
    const view = open(base, { wrapper: ({ children }) => <React.StrictMode>{children}</React.StrictMode> });
    view.edit({ mic_muted: true });
    const ticket = view.result.current.beginSave();
    expect(view.accept(ticket, { ...base, mic_muted: true }).applied).toBe(true);
    expect(view.result.current.hasChanges).toBe(false);
  });
});
