import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import QueueModal from './QueueModal';
import { apiFetch, useViolaApi } from '../hooks/useViolaApi';
import wire from './fixtures/queueWireContract.json';
vi.mock('../lib/gotrue_client',()=>({getGoTrueAccessToken:vi.fn(async()=> '')}));
const track=wire.get_success.body.data.queue[0];
function response(name,status=wire[name].status){return {ok:status>=200&&status<300,status,json:async()=>structuredClone(wire[name].body),text:async()=>JSON.stringify(wire[name].body)};}
beforeEach(()=>{window.__VIOLA_API_KEY__='synthetic-noncredential-fixture';});
afterEach(()=>{cleanup();vi.unstubAllGlobals();delete window.__VIOLA_API_KEY__;});
const actions=[['clear','Clear Queue','Could not clear queue. Please try again.'],['play',`Play ${track.title} now`,'Could not play item. Please try again.'],['remove',`Remove ${track.title} from queue`,'Could not remove item. Please try again.']];
describe('Actual serialized Queue route envelopes through apiFetch and QueueModal',()=>{
  it('loads the real serialized GET data without inventing an inner ok field',async()=>{
    expect(wire.get_success.body.data).not.toHaveProperty('ok');vi.stubGlobal('fetch',vi.fn(async()=>response('get_success')));
    await act(async()=>render(<QueueModal isOpen onClose={()=>{}} />));expect(screen.getByText(track.title)).toBeInTheDocument();
  });
  it.each(actions)('accepts genuine serialized %s success with an empty data object',async(action,label)=>{
    expect(wire[action+'_success'].body.data).toEqual({});const fetch=vi.fn(async(_url,options)=>response(options.method?action+'_success':'get_success'));vi.stubGlobal('fetch',fetch);
    await act(async()=>render(<QueueModal isOpen onClose={()=>{}} wsQueue={[track]} />));await act(async()=>fireEvent.click(screen.getByRole('button',{name:label})));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();expect(screen.getByRole('button',{name:'Clear Queue'})).toBeEnabled();
    expect(fetch.mock.calls.filter(([_url,options])=>!options.method)).toHaveLength(action==='clear'?1:2);
  });
  it.each(actions)('keeps actual non2xx %s refusal visible',async(action,label,error)=>{
    vi.stubGlobal('fetch',vi.fn(async(_url,options)=>response(options.method?action+'_refused':'get_success')));
    await act(async()=>render(<QueueModal isOpen onClose={()=>{}} wsQueue={[track]} />));await act(async()=>fireEvent.click(screen.getByRole('button',{name:label})));
    expect(screen.getByRole('alert')).toHaveTextContent(error);expect(screen.getByText(track.title)).toBeInTheDocument();
  });
  it.each(actions)('does not launder an explicit %s refusal even under a faulty HTTP200 status',async(action,label,error)=>{
    vi.stubGlobal('fetch',vi.fn(async(_url,options)=>options.method?response(action+'_refused',200):response('get_success')));
    await act(async()=>render(<QueueModal isOpen onClose={()=>{}} wsQueue={[track]} />));await act(async()=>fireEvent.click(screen.getByRole('button',{name:label})));
    expect(screen.getByRole('alert')).toHaveTextContent(error);expect(screen.getByText(track.title)).toBeInTheDocument();
  });
  it('retains explicit GET refusal in the Queue method and rejects transport failure',async()=>{
    const fetch=vi.fn().mockResolvedValueOnce(response('get_refused',200)).mockResolvedValueOnce(response('get_refused'));vi.stubGlobal('fetch',fetch);
    const {result}=renderHook(()=>useViolaApi());await expect(result.current.getQueue()).resolves.toMatchObject({ok:false});await expect(result.current.getQueue()).rejects.toMatchObject({status:500});
  });
  it('preserves default apiFetch unwrapping for unrelated consumers',async()=>{
    vi.stubGlobal('fetch',vi.fn(async()=>response('get_success')));await expect(apiFetch('/synthetic/unrelated')).resolves.toEqual(wire.get_success.body.data);
  });
});


describe('Queue rejects malformed successful envelope data', () => {
  const malformed = [null, false, [], 'invalid', 0];
  it.each(actions.flatMap(([action, label, error]) => malformed.map(data => ({action, label, error, data}))))('rejects $action data $data without changing confirmed state', async ({label, error, data}) => {
      vi.stubGlobal('fetch', vi.fn(async (_url, options) => options.method
        ? { ok: true, status: 200, json: async () => ({ ok: true, data }) }
        : response('get_success')));
      await act(async () => render(<QueueModal isOpen onClose={() => {}} wsQueue={[track]} />));
      await act(async () => fireEvent.click(screen.getByRole('button', { name: label })));
      expect(screen.getByRole('alert')).toHaveTextContent(error);
      expect(screen.getByText(track.title)).toBeInTheDocument();
    });
});
