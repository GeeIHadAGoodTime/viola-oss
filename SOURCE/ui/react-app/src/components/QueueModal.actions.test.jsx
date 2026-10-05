import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import QueueModal from './QueueModal';
vi.mock('../lib/gotrue_client', () => ({getGoTrueAccessToken: vi.fn(async () => '')}));
const track = {id:'synthetic-queue-track',title:'Synthetic queued song'};
function response(body) {return {ok:true,status:200,json:async()=>body};}
beforeEach(() => {window.__VIOLA_API_KEY__='synthetic-noncredential-fixture';});
afterEach(() => {cleanup();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals();delete window.__VIOLA_API_KEY__;});
describe('Queue action refusal through the real API adapter', () => {
  it.each([
    ['clear','Clear Queue','Could not clear queue. Please try again.'],
    ['remove',`Remove ${track.title} from queue`,'Could not remove item. Please try again.'],
    ['play',`Play ${track.title} now`,'Could not play item. Please try again.'],
  ])('preserves confirmed data and exposes a refused %s operation', async (_action,label,error) => {
    const fetch = vi.fn(async (_url,options) => response(options.method ? {ok:false,error:{code:'not_applied'}} : {ok:true,queue:[track]}));
    vi.stubGlobal('fetch',fetch);
    render(<QueueModal isOpen onClose={()=>{}} />);
    expect(await screen.findByText(track.title)).toBeInTheDocument();
    await act(async()=>{fireEvent.click(screen.getByRole('button',{name:label}));});
    expect(screen.getByText(error)).toBeInTheDocument();
    expect(screen.getByText(track.title)).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([_url,options])=>!options.method)).toHaveLength(1);
  });
});

describe('Queue action ownership across dismissal', () => {
  it('does not clear a reopened queue from an older successful Clear response', async () => {
    let resolveClear;
    let reads=0;
    const newer={id:'synthetic-newer-track',title:'Synthetic newer queued song'};
    vi.stubGlobal('fetch',vi.fn(async (_url,options)=>{
      if(options.method)return new Promise(resolve=>{resolveClear=resolve;});
      return response({ok:true,queue:++reads===1?[track]:[newer]});
    }));
    const view=render(<QueueModal isOpen onClose={()=>{}} />);
    await screen.findByText(track.title);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Clear Queue'})));
    view.rerender(<QueueModal isOpen={false} onClose={()=>{}} />);
    view.rerender(<QueueModal isOpen onClose={()=>{}} />);
    await screen.findByText(newer.title);
    await act(async()=>resolveClear(response({ok:true,error:null})));
    expect(screen.getByText(newer.title)).toBeInTheDocument();
  });
});

const actionCases = [
  ['clear','Clear Queue','Could not clear queue. Please try again.'],
  ['remove',`Remove ${track.title} from queue`,'Could not remove item. Please try again.'],
  ['play',`Play ${track.title} now`,'Could not play item. Please try again.'],
];
describe('Queue action acknowledgement shape and preserved success', () => {
  it.each(actionCases)('accepts a wrapped canonical %s acknowledgement', async (action,label) => {
    const fetch=vi.fn(async (_url,options)=>response(options.method ? {ok:true,data:{ok:true,error:null}} : {ok:true,queue:[track]}));
    vi.stubGlobal('fetch',fetch);
    render(<QueueModal isOpen onClose={()=>{}} />);await screen.findByText(track.title);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:label})));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    if(action==='clear')expect(screen.queryByText(track.title)).not.toBeInTheDocument();
    else expect(fetch.mock.calls.filter(([_url,options])=>!options.method)).toHaveLength(2);
  });
  it.each([null,{}, {ok:'true'}, {ok:true,data:{ok:false,error:'refused'}}])('does not clear confirmed data for missing/malformed acknowledgement %j', async body => {
    vi.stubGlobal('fetch',vi.fn(async (_url,options)=>response(options.method ? body : {ok:true,queue:[track]})));
    render(<QueueModal isOpen onClose={()=>{}} />);await screen.findByText(track.title);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Clear Queue'})));
    expect(screen.getByText('Could not clear queue. Please try again.')).toBeInTheDocument();
    expect(screen.getByText(track.title)).toBeInTheDocument();
  });
  it.each(actionCases)('preserves existing HTTP failure feedback for %s', async (_action,label,error) => {
    vi.stubGlobal('fetch',vi.fn(async (_url,options)=>options.method ? {ok:false,status:503,text:async()=>'{"ok":false}'} : response({ok:true,queue:[track]})));
    render(<QueueModal isOpen onClose={()=>{}} />);await screen.findByText(track.title);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:label})));
    expect(screen.getByText(error)).toBeInTheDocument();expect(screen.getByText(track.title)).toBeInTheDocument();
  });
});

describe('Queue action bounded ownership', () => {
  it('admits only one same-tick action', async () => {
    const fetch=vi.fn(async (_url,options)=> options.method ? new Promise(()=>{}) : response({ok:true,queue:[track]}));
    vi.stubGlobal('fetch',fetch);render(<QueueModal isOpen onClose={()=>{}} />);await screen.findByText(track.title);
    const button=screen.getByRole('button',{name:'Clear Queue'});
    await act(async()=>{fireEvent.click(button);fireEvent.click(button);});
    expect(fetch.mock.calls.filter(([_url,options])=>options.method)).toHaveLength(1);
    expect(screen.getByRole('button',{name:'Working...'})).toBeDisabled();
  });
  it('exposes uncertainty, lets a newer request proceed, and ignores the old completion', async () => {
    vi.useFakeTimers({toFake:['setTimeout','clearTimeout','performance']});
    const completions=[];
    vi.stubGlobal('fetch',vi.fn(async (_url,options)=>options.method ? new Promise(resolve=>completions.push(resolve)) : response({ok:true,queue:[track]})));
    await act(async()=>render(<QueueModal isOpen onClose={()=>{}} />));
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Clear Queue'})));
    act(()=>vi.advanceTimersByTime(15000));
    expect(screen.getByRole('alert')).toHaveTextContent('result is unknown');
    expect(screen.getByText(track.title)).toBeInTheDocument();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Clear Queue'})));
    await act(async()=>completions[0](response({ok:true,error:null})));
    expect(screen.getByRole('button',{name:'Working...'})).toBeDisabled();
    expect(screen.getByText(track.title)).toBeInTheDocument();
    await act(async()=>completions[1](response({ok:true,error:null})));
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();
  });
  it('rejects expired success even before the delayed timer callback runs', async () => {
    vi.useFakeTimers({toFake:['setTimeout','clearTimeout']});let resolve;
    const clock=vi.spyOn(performance,'now').mockReturnValue(100);
    vi.stubGlobal('fetch',vi.fn(async (_url,options)=>options.method ? new Promise(done=>{resolve=done;}) : response({ok:true,queue:[track]})));
    await act(async()=>render(<QueueModal isOpen onClose={()=>{}} />));
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Clear Queue'})));
    clock.mockReturnValue(15100);
    await act(async()=>resolve(response({ok:true,error:null})));
    expect(screen.getByRole('alert')).toHaveTextContent('result is unknown');
    expect(screen.getByText(track.title)).toBeInTheDocument();
  });
  it('cleans up pending work on unmount and preserves StrictMode current actions', async () => {
    vi.useFakeTimers({toFake:['setTimeout','clearTimeout','performance']});let resolve;
    vi.stubGlobal('fetch',vi.fn(async (_url,options)=>options.method ? new Promise(done=>{resolve=done;}) : response({ok:true,queue:[track]})));
    let view;await act(async()=>{view=render(<StrictMode><QueueModal isOpen onClose={()=>{}} /></StrictMode>);});
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Clear Queue'})));
    expect(screen.getByRole('button',{name:'Working...'})).toBeDisabled();view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await act(async()=>resolve(response({ok:true,error:null})));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
