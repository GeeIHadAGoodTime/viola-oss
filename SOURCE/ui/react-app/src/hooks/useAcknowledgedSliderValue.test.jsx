import React from 'react';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ACKNOWLEDGEMENT_TIMEOUT_MS, useAcknowledgedSliderValue } from './useAcknowledgedSliderValue';
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const mount = (commit, server = 60, options = {}) => renderHook(({ serverValue, save }) => useAcknowledgedSliderValue(serverValue, save), { initialProps: { serverValue: server, save: commit }, ...options });
const input = (view, value, flush = true) => act(() => { view.result.current[1](value); if (flush) view.result.current[2](); });
beforeEach(() => vi.useFakeTimers());
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe('acknowledged Rooms slider ownership', () => {
  it('shows pending intent, then adopts the exact normalized zero acknowledgement', async () => {
    const request=deferred(), save=vi.fn(() => request.promise), view=mount(save);
    input(view, 4); expect(view.result.current[0]).toBe(4); expect(view.result.current[4]).toBe(true);
    await act(async () => request.resolve({ok:true,value:0}));
    expect(view.result.current[0]).toBe(0); expect(view.result.current[3]).toBeNull(); expect(view.result.current[4]).toBe(false);
  });
  it.each(['refused','rejected','thrown','malformed'])('restores confirmed state after %s and permits retry', async kind => {
    const save=vi.fn(() => { if(kind==='thrown') throw Error('synthetic'); if(kind==='rejected') return Promise.reject(Error('synthetic')); return Promise.resolve(kind==='malformed'?{ok:'yes'}:{ok:false}); });
    const view=mount(save); input(view,10); await act(async()=>{});
    expect(view.result.current[0]).toBe(60); expect(view.result.current[3]).toMatch(/Couldn't save/); expect(view.result.current[4]).toBe(false);
    save.mockResolvedValue({ok:true,value:10}); input(view,10); await act(async()=>{});
    expect(view.result.current[0]).toBe(10); expect(view.result.current[3]).toBeNull(); expect(save).toHaveBeenCalledTimes(2);
  });
  for (let mask=0;mask<8;mask++) it(`serializes all three-write outcomes ${mask}`, async () => {
    const requests=[deferred(),deferred(),deferred()];let index=0;const save=vi.fn(()=>requests[index++].promise),view=mount(save);
    input(view,10);input(view,20);input(view,30);expect(save).toHaveBeenCalledTimes(1);let confirmed=60;
    for(let i=0;i<3;i++){
      const accepted=Boolean(mask & (1<<i));if(accepted)confirmed=(i+1)*10;
      await act(async()=>requests[i].resolve({ok:accepted,value:(i+1)*10}));
      expect(save).toHaveBeenCalledTimes(Math.min(i+2,3));
      expect(view.result.current[0]).toBe(i<2?30:confirmed);
    }
    expect(save.mock.calls.map(call=>call[0])).toEqual([10,20,30]);expect(view.result.current[4]).toBe(false);
  });
  it('keeps a later uncommitted drag when an older request fails', async () => {
    const request=deferred(),save=vi.fn(()=>request.promise),view=mount(save);input(view,10);input(view,20,false);
    await act(async()=>request.resolve({ok:false}));expect(view.result.current[0]).toBe(20);expect(view.result.current[3]).toBeNull();expect(view.result.current[4]).toBe(true);
    save.mockResolvedValue({ok:true,value:20});await act(async()=>vi.advanceTimersByTime(50));expect(view.result.current[0]).toBe(20);
  });
  it('retains newest intent through an earlier server echo and restores a current server value on failure', async () => {
    const request=deferred(),save=vi.fn(()=>request.promise),view=mount(save);input(view,10);
    act(()=>view.rerender({serverValue:42,save}));expect(view.result.current[0]).toBe(10);
    await act(async()=>request.resolve({ok:false}));expect(view.result.current[0]).toBe(42);
  });
  it('bounds an unacknowledged request and ignores its late success after a retry', async () => {
    const request=deferred(),save=vi.fn(()=>request.promise),view=mount(save);input(view,10);
    await act(async()=>vi.advanceTimersByTime(ACKNOWLEDGEMENT_TIMEOUT_MS));expect(view.result.current[0]).toBe(60);expect(view.result.current[3]).toMatch(/not confirmed/);expect(view.result.current[4]).toBe(false);
    save.mockResolvedValue({ok:true,value:20});input(view,20);await act(async()=>{});
    await act(async()=>request.resolve({ok:true,value:10}));expect(view.result.current[0]).toBe(20);expect(view.result.current[3]).toBeNull();
  });
  it('continues a queued deliberate choice after earlier acknowledgement times out', async () => {
    const first=deferred(),second=deferred(),save=vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise),view=mount(save);input(view,10);input(view,20);
    await act(async()=>vi.advanceTimersByTime(ACKNOWLEDGEMENT_TIMEOUT_MS));expect(save).toHaveBeenCalledTimes(2);expect(view.result.current[0]).toBe(20);
    await act(async()=>second.resolve({ok:true,value:20}));await act(async()=>first.reject(Error('late')));expect(view.result.current[0]).toBe(20);expect(view.result.current[3]).toBeNull();
  });
  it('flushes and orders the final deliberate drag on unmount', async () => {
    const first=deferred(),second=deferred(),save=vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise),view=mount(save);input(view,10);input(view,20,false);view.unmount();
    expect(save).toHaveBeenCalledTimes(1);await act(async()=>first.resolve({ok:true,value:10}));expect(save.mock.calls.map(x=>x[0])).toEqual([10,20]);await act(async()=>second.resolve({ok:true,value:20}));expect(vi.getTimerCount()).toBe(0);
  });
  it('uses the latest callback when the pending drag is flushed', () => {
    const first=vi.fn(),second=vi.fn(),view=mount(first);input(view,10,false);view.rerender({serverValue:60,save:second});act(()=>view.result.current[2]());expect(first).not.toHaveBeenCalled();expect(second).toHaveBeenCalledWith(10);
  });
  it('keeps the active session functional through StrictMode replay', async () => {
    const save=vi.fn().mockResolvedValue({ok:true,value:0}),view=mount(save,60,{wrapper:({children})=><React.StrictMode>{children}</React.StrictMode>});input(view,0);await act(async()=>{});expect(save).toHaveBeenCalledTimes(1);expect(view.result.current[0]).toBe(0);
  });
  it('checks the monotonic deadline even when the timeout callback has not executed', async () => {
    let now = 0;
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => now);
    try {
      const request = deferred(), view = mount(() => request.promise);
      input(view, 10);
      now = ACKNOWLEDGEMENT_TIMEOUT_MS + 1;
      await act(async () => request.resolve({ ok: true, value: 10 }));
      expect(view.result.current[0]).toBe(60);
      expect(view.result.current[3]).toMatch(/not confirmed/);
      expect(view.result.current[4]).toBe(false);
    } finally { clock.mockRestore(); }
  });
  it('honors an explicitly retired adapter receipt while its own timer is still pending', async () => {
    const request = deferred(), view = mount(() => request.promise);
    input(view, 10);
    await act(async () => request.resolve({ ok: true, value: 10, uiCurrent: false }));
    expect(view.result.current[0]).toBe(60);
    expect(view.result.current[3]).toMatch(/not confirmed/);
    expect(view.result.current[4]).toBe(false);
  });

  it('cannot release a newer in-flight request when a retired reply arrives', async () => {
    const first = deferred(), second = deferred(), third = deferred();
    const save = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise).mockReturnValueOnce(third.promise);
    const view = mount(save);
    input(view, 10); input(view, 20);
    await act(async () => vi.advanceTimersByTime(ACKNOWLEDGEMENT_TIMEOUT_MS));
    input(view, 30);
    await act(async () => first.resolve({ ok: true, value: 10 }));
    expect(save.mock.calls.map(call => call[0])).toEqual([10, 20]);
    expect(view.result.current[0]).toBe(30);
    await act(async () => second.resolve({ ok: true, value: 20 }));
    expect(save.mock.calls.map(call => call[0])).toEqual([10, 20, 30]);
    await act(async () => third.resolve({ ok: true, value: 30 }));
    expect(view.result.current[0]).toBe(30);
    expect(view.result.current[3]).toBeNull();
  });

  it('does not hold a fresh server value behind the settle window after a failed write', async () => {
    const save = vi.fn().mockResolvedValue({ ok: false }), view = mount(save);
    input(view, 10);
    await act(async () => {});
    expect(view.result.current[0]).toBe(60);
    act(() => view.rerender({ serverValue: 42, save }));
    expect(view.result.current[0]).toBe(42);
  });

});
