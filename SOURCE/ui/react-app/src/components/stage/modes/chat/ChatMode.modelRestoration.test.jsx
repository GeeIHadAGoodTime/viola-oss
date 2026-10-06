import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../../../test/test-utils';
import ChatMode from './ChatMode';

vi.mock('../../../../hooks/useWebSocket', () => ({ useWebSocket: () => ({}) }));
const deferred = () => {
  let resolve;const promise=new Promise(accept=>{resolve=accept;});return {promise,resolve};
};
const ok = data => new Response(JSON.stringify({ ok:true, data, error:null }), {
  status:200, headers:{'Content-Type':'application/json'},
});

describe('Chat thread model restoration through actual apiFetch', () => {
  let thread, otherThread, sent, sentTargets, unexpected, readThread, patchModel, modelCatalog;
  beforeEach(() => {
    thread={id:'stored-thread',title:'Stored synthetic conversation',model:'model-b'};
    sent=[];sentTargets=[];unexpected=[];otherThread=null;
    modelCatalog=vi.fn(async()=>ok({current_model:'model-a',provider:'Synthetic',models:['model-a','model-b','model-c']}));
    readThread=vi.fn(async(path)=>ok({thread:path.endsWith('/other-thread')?otherThread:thread,messages:[],active_stream_ids:[]}));
    patchModel=vi.fn(async(options)=>{thread={...thread,...JSON.parse(options.body)};return ok({thread});});
    window.viola={};window.__VIOLA_API_KEY__='synthetic-reopen-probe';
    vi.stubGlobal('fetch',async (path,options={}) => {
      if(path==='/v1/chat/models')return modelCatalog();
      if(path==='/v1/chat/threads' || path.startsWith('/v1/chat/threads?'))return ok({threads:[thread,otherThread].filter(Boolean)});
      if(path.startsWith('/v1/chat/threads/') && path.endsWith('/send')) {
        sent.push(JSON.parse(options.body));sentTargets.push(path);
        return new Response(JSON.stringify({ok:false,data:null,error:{code:'synthetic_refusal',message:'No provider call'}}),{status:400});
      }
      if(path==='/v1/chat/threads/stored-thread' && options.method==='PATCH') {
        return patchModel(options);
      }
      if(path==='/v1/chat/threads/stored-thread' || path==='/v1/chat/threads/other-thread')return readThread(path);
      unexpected.push(path);throw new Error('Unexpected synthetic route');
    });
  });
  afterEach(() => {
    expect(unexpected).toEqual([]);vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals();
    delete window.viola;delete window.__VIOLA_API_KEY__;
  });
  async function mount() {
    render(<ChatMode principalKey="synthetic-owner"/>);
    await screen.findByDisplayValue('Stored synthetic conversation');
    const select=screen.getByRole('combobox',{name:'Model'});
    await waitFor(()=>expect(select.options.length).toBeGreaterThan(2));
    return select;
  }
  async function send() {
    await act(async()=>{
      fireEvent.change(screen.getByRole('textbox',{name:'Message Viola'}),{target:{value:'Synthetic message'}});
    });
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    await waitFor(()=>expect(sent).toHaveLength(1));
  }
  it('shows the persisted thread choice instead of the global provider default',async()=>{
    expect(await mount()).toHaveValue('model-b');
  });
  it('sends the reopened conversation through its persisted model',async()=>{
    await mount();await send();expect(sent[0]).toEqual({text:'Synthetic message',model:'model-b'});
  });
  it('preserves a thread that already matches the global default',async()=>{
    thread.model='model-a';expect(await mount()).toHaveValue('model-a');await send();expect(sent[0].model).toBe('model-a');
  });
  it('uses a newly acknowledged explicit model when sending',async()=>{
    const select=await mount();await act(async()=>fireEvent.change(select,{target:{value:'model-c'}}));
    expect(select).toHaveValue('model-c');await send();expect(sent[0].model).toBe('model-c');
  });
  it.each([null, '', 'stored-normalized-model'])('adopts exact persisted %s and uses it on Send',async model=>{
    thread.model=model;const select=await mount();expect(select).toHaveValue(model??'');
    await send();expect(sent[0].model).toBe(model||null);
  });

  it('keeps a restored model while refreshing the provider catalog',async()=>{
    thread.model='stored-normalized-model';const select=await mount();
    await act(async()=>fireEvent.focus(select));
    expect(select).toHaveValue('stored-normalized-model');
    expect(screen.getByRole('option',{name:'Synthetic - model-c'})).toBeInTheDocument();
  });

  it('keeps the restored value visible through a failed catalog and Retry',async()=>{
    const select=await mount();modelCatalog.mockRejectedValueOnce(new TypeError('Synthetic catalog unavailable'));
    await act(async()=>fireEvent.focus(select));expect(select).toHaveValue('model-b');expect(select).toBeDisabled();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Retry model list'})));
    expect(select).toHaveValue('model-b');expect(select).toBeEnabled();
  });

  it('does not let an older thread read replace a newer model acknowledgement',async()=>{
    const pending=deferred();const oldSnapshot={...thread};readThread.mockReturnValueOnce(pending.promise);
    render(<ChatMode principalKey="synthetic-owner"/>);
    await waitFor(()=>expect(readThread).toHaveBeenCalledTimes(1));
    const select=screen.getByRole('combobox',{name:'Model'});
    await act(async()=>fireEvent.change(select,{target:{value:'model-c'}}));expect(select).toHaveValue('model-c');
    await act(async()=>pending.resolve(ok({thread:oldSnapshot,messages:[],active_stream_ids:[]})));
    expect(select).toHaveValue('model-c');await send();expect(sent[0].model).toBe('model-c');
  });

  it('accepts the known read baseline during a pending save and then adopts its acknowledgement',async()=>{
    const read=deferred();const save=deferred();const oldSnapshot={...thread};
    readThread.mockReturnValueOnce(read.promise);patchModel.mockReturnValueOnce(save.promise);
    render(<ChatMode principalKey="synthetic-owner"/>);await waitFor(()=>expect(readThread).toHaveBeenCalledTimes(1));
    const select=screen.getByRole('combobox',{name:'Model'});
    await act(async()=>fireEvent.change(select,{target:{value:'model-c'}}));expect(select).toBeDisabled();
    await act(async()=>read.resolve(ok({thread:oldSnapshot,messages:[],active_stream_ids:[]})));
    expect(select).toHaveValue('model-b');expect(select).toBeDisabled();
    await act(async()=>save.resolve(ok({thread:{...thread,model:'model-c'}})));
    expect(select).toHaveValue('model-c');expect(select).toBeEnabled();
  });

  it('retires an older principal read even when the next principal repeats the thread id',async()=>{
    const pending=deferred();const oldSnapshot={...thread};readThread.mockReturnValueOnce(pending.promise);
    const view=render(<ChatMode principalKey="synthetic-owner"/>);await waitFor(()=>expect(readThread).toHaveBeenCalledTimes(1));
    thread={...thread,model:'model-c'};
    await act(async()=>view.rerender(<ChatMode principalKey="next-synthetic-owner"/>));
    const select=screen.getByRole('combobox',{name:'Model'});await waitFor(()=>expect(select).toHaveValue('model-c'));
    await act(async()=>pending.resolve(ok({thread:oldSnapshot,messages:[],active_stream_ids:[]})));
    expect(select).toHaveValue('model-c');
  });

  it('restores the target thread model and retires an A to B to A delayed read',async()=>{
    otherThread={id:'other-thread',title:'Other synthetic conversation',model:'model-c'};
    const select=await mount();expect(select).toHaveValue('model-b');
    const pending=deferred();readThread.mockReturnValueOnce(pending.promise);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Other synthetic conversation',exact:true})));
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Stored synthetic conversation',exact:true})));
    await waitFor(()=>expect(select).toHaveValue('model-b'));
    await act(async()=>pending.resolve(ok({thread:otherThread,messages:[],active_stream_ids:[]})));
    expect(select).toHaveValue('model-b');await send();expect(sentTargets[0]).toBe('/v1/chat/threads/stored-thread/send');
  });

  it('restores a newly selected thread model when its read completes',async()=>{
    otherThread={id:'other-thread',title:'Other synthetic conversation',model:'model-c'};
    const select=await mount();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Other synthetic conversation',exact:true})));
    await screen.findByDisplayValue('Other synthetic conversation');expect(select).toHaveValue('model-c');
    await send();expect(sentTargets[0]).toBe('/v1/chat/threads/other-thread/send');expect(sent[0].model).toBe('model-c');
  });

  it('preserves a fast restored snapshot across StrictMode layout replay',async()=>{
    render(<StrictMode><ChatMode principalKey="synthetic-owner"/></StrictMode>);
    await screen.findByDisplayValue('Stored synthetic conversation');
    const select=screen.getByRole('combobox',{name:'Model'});expect(select).toHaveValue('model-b');
    await act(async()=>fireEvent.focus(select));expect(select).toHaveValue('model-b');
  });

  async function selectPendingTarget() {
    otherThread={id:'other-thread',title:'Other synthetic conversation',model:'model-c'};
    await mount();
    fireEvent.change(screen.getByRole('textbox',{name:'Message Viola'}),{target:{value:'Keep this draft'}});
    const pending=deferred();readThread.mockReturnValueOnce(pending.promise);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Other synthetic conversation',exact:true})));
    return pending;
  }

  it('keeps the draft and blocks Send until the selected conversation model arrives',async()=>{
    const pending=await selectPendingTarget();
    expect(screen.getByRole('status')).toHaveTextContent('Loading this conversation');
    const sendButton=screen.getByRole('button',{name:'Send message'});expect(sendButton).toBeDisabled();
    fireEvent.click(sendButton);fireEvent.keyDown(screen.getByRole('textbox',{name:'Message Viola'}),{key:'Enter'});
    expect(sent).toEqual([]);expect(screen.getByRole('textbox',{name:'Message Viola'})).toHaveValue('Keep this draft');
    await act(async()=>pending.resolve(ok({thread:otherThread,messages:[],active_stream_ids:[]})));
    expect(sendButton).toBeEnabled();expect(screen.getByRole('combobox',{name:'Model'})).toHaveValue('model-c');
    await act(async()=>fireEvent.click(sendButton));
    expect(sent).toEqual([{text:'Keep this draft',model:'model-c'}]);expect(sentTargets).toEqual(['/v1/chat/threads/other-thread/send']);
  });

  it('blocks a same-batch Send before the selected-thread render commits',async()=>{
    otherThread={id:'other-thread',title:'Other synthetic conversation',model:'model-c'};
    await mount();fireEvent.change(screen.getByRole('textbox',{name:'Message Viola'}),{target:{value:'Keep this draft'}});
    const pending=deferred();readThread.mockReturnValueOnce(pending.promise);
    await act(async()=>{
      fireEvent.click(screen.getByRole('button',{name:'Other synthetic conversation',exact:true}));
      fireEvent.click(screen.getByRole('button',{name:'Send message'}));
    });
    expect(sent).toEqual([]);expect(screen.getByRole('textbox',{name:'Message Viola'})).toHaveValue('Keep this draft');
    await act(async()=>pending.resolve(ok({thread:otherThread,messages:[],active_stream_ids:[]})));
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    expect(sent[0].model).toBe('model-c');
  });

  it.each([400,404,503])('keeps the draft through a serialized %s thread-read failure and Retry',async status=>{
    const pending=await selectPendingTarget();
    await act(async()=>pending.resolve(new Response(JSON.stringify({ok:false,data:null,error:{code:'read_failed',message:'Synthetic read failure'}}),{status})));
    expect(screen.getByRole('alert')).toHaveTextContent('Your draft is kept');
    expect(screen.getByRole('button',{name:'Send message'})).toBeDisabled();expect(sent).toEqual([]);
    expect(screen.getByRole('textbox',{name:'Message Viola'})).toHaveValue('Keep this draft');
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Retry conversation'})));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    expect(sent).toEqual([{text:'Keep this draft',model:'model-c'}]);
  });

  it('expires an unanswered target read, permits Retry and ignores its late response',async()=>{
    const pending=await selectPendingTarget();vi.useFakeTimers();
    // Start a new owned read under the controllable clock.
    const timed=deferred();readThread.mockReturnValueOnce(timed.promise);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Other synthetic conversation',exact:true})));
    await act(async()=>vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole('alert')).toHaveTextContent('Retry before sending');
    expect(screen.getByRole('button',{name:'Send message'})).toBeDisabled();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Retry conversation'})));
    expect(screen.getByRole('combobox',{name:'Model'})).toHaveValue('model-c');
    await act(async()=>{
      timed.resolve(ok({thread:{...otherThread,model:'model-a'},messages:[],active_stream_ids:[]}));
      pending.resolve(ok({thread:{...otherThread,model:'model-b'},messages:[],active_stream_ids:[]}));
    });
    expect(screen.getByRole('combobox',{name:'Model'})).toHaveValue('model-c');
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    expect(sent[0].model).toBe('model-c');
  });

  it('rejects settlement after the deadline even before the timer callback runs',async()=>{
    const pending=await selectPendingTarget();const now=performance.now();
    vi.spyOn(performance,'now').mockReturnValue(now+15001);
    await act(async()=>pending.resolve(ok({thread:otherThread,messages:[],active_stream_ids:[]})));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not confirm this conversation');
    expect(screen.getByRole('button',{name:'Send message'})).toBeDisabled();expect(sent).toEqual([]);
  });

  it('does not accept a different thread snapshot as completion of the selected read',async()=>{
    const pending=await selectPendingTarget();
    await act(async()=>pending.resolve(ok({thread,messages:[],active_stream_ids:[]})));
    expect(screen.getByRole('alert')).toHaveTextContent('Retry before sending');
    expect(screen.getByRole('button',{name:'Send message'})).toBeDisabled();expect(sent).toEqual([]);
  });

  it('retires a target error when the user returns to a confirmed conversation',async()=>{
    const pending=await selectPendingTarget();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Stored synthetic conversation',exact:true})));
    await act(async()=>pending.resolve(new Response(JSON.stringify({ok:false,error:{code:'failed',message:'Late failure'}}),{status:503})));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    expect(sentTargets[0]).toBe('/v1/chat/threads/stored-thread/send');expect(sent[0].model).toBe('model-b');
  });

  it('uses the latest acknowledged target model after the read has completed',async()=>{
    const pending=deferred();const oldSnapshot={...thread};readThread.mockReturnValueOnce(pending.promise);
    render(<ChatMode principalKey="synthetic-owner"/>);await waitFor(()=>expect(readThread).toHaveBeenCalledTimes(1));
    await act(async()=>fireEvent.change(screen.getByRole('combobox',{name:'Model'}),{target:{value:'model-c'}}));
    await act(async()=>pending.resolve(ok({thread:oldSnapshot,messages:[],active_stream_ids:[]})));
    await send();expect(sent[0].model).toBe('model-c');
  });

  it('blocks suggestion dispatch while the new target read is pending',async()=>{
    const pending=await selectPendingTarget();
    const suggestion=screen.getByRole('button',{name:'Plan the next stage of this project'});
    await act(async()=>fireEvent.click(suggestion));expect(sent).toEqual([]);
    expect(screen.getByRole('textbox',{name:'Message Viola'})).toHaveValue('Plan the next stage of this project');
    await act(async()=>pending.resolve(ok({thread:otherThread,messages:[],active_stream_ids:[]})));
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    expect(sent[0]).toEqual({text:'Plan the next stage of this project',model:'model-c'});
  });

  it('allows a confirmed conversation to send during a same-target refresh and retires that read',async()=>{
    await mount();fireEvent.change(screen.getByRole('textbox',{name:'Message Viola'}),{target:{value:'Known target'}});
    const pending=deferred();readThread.mockReturnValueOnce(pending.promise);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Stored synthetic conversation',exact:true})));
    expect(screen.getByRole('button',{name:'Send message'})).toBeEnabled();
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Send message'})));
    expect(sent[0]).toEqual({text:'Known target',model:'model-b'});
    expect(screen.queryByText('Refreshing this conversation...')).not.toBeInTheDocument();
    await act(async()=>pending.resolve(ok({thread:{...thread,model:'model-a'},messages:[],active_stream_ids:[]})));
    expect(screen.getByRole('combobox',{name:'Model'})).toHaveValue('model-b');
  });

  it('clears its pending read deadline on unmount',async()=>{
    otherThread={id:'other-thread',title:'Other synthetic conversation',model:'model-c'};
    const view=render(<ChatMode principalKey="synthetic-owner"/>);
    await screen.findByDisplayValue('Stored synthetic conversation');vi.useFakeTimers();
    const pending=deferred();readThread.mockReturnValueOnce(pending.promise);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Other synthetic conversation',exact:true})));
    expect(vi.getTimerCount()).toBeGreaterThan(0);view.unmount();await act(async()=>vi.advanceTimersByTimeAsync(0));
    expect(vi.getTimerCount()).toBe(0);
    await act(async()=>pending.resolve(ok({thread:otherThread,messages:[],active_stream_ids:[]})));
    expect(sent).toEqual([]);
  });

});
