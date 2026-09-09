import { describe, it, expect } from 'vitest';
import { resolveStageContentMode } from './SmartDisplay';

// Oracle for the stage-routing rule (2026-06-29, reversed from 2026-06-24):
//  - a live phone call does NOT auto-open / auto-steal the phone stage (founder
//    direction: placing a call must not switch the stage out from under the user),
//  - BUT while the user IS on the phone tab during a call, the phone stage is HELD
//    so a concurrent agentic-task/browser signal can't flip it mid-call,
//  - the browser webview shows ONLY for genuine web browsing, never an empty
//    about:blank panel for non-browsing agent tasks.
describe('resolveStageContentMode', () => {
  it('does NOT steal the stage for a live call when the user is elsewhere', () => {
    // Founder direction 2026-06-29: a live call must NOT pull the stage to phone.
    // The user stays on music / browser / agentic task; only the phone pill +
    // window title signal the live call. The live-call screen renders when the
    // user opens the phone tab (activeCallId-driven), not by forcing it here.
    expect(resolveStageContentMode('now_playing', { activeCallId: 'call_1' })).toBe('music');
    expect(resolveStageContentMode('agentic_task', { activeCallId: 'call_1', browserModeActive: true })).toBe('browser');
    expect(resolveStageContentMode('browser', { activeCallId: 'call_1' })).toBe('browser');
    expect(resolveStageContentMode('chat', { activeCallId: 'call_1' })).toBe('chat');
  });

  it('HOLDS the phone stage when the user is on the phone tab during a live call', () => {
    // Once the user is viewing the phone tab mid-call, the live transcript +
    // takeover must not be flipped away by a concurrent agentic-task/browser
    // signal.
    expect(resolveStageContentMode('phone_call', { activeCallId: 'call_1' })).toBe('phone');
    expect(resolveStageContentMode('phone_tab', { activeCallId: 'call_1' })).toBe('phone');
  });

  it('does NOT show the browser webview for a non-browsing agentic task', () => {
    expect(resolveStageContentMode('agentic_task', {})).toBe('music');
    expect(resolveStageContentMode('agentic_task', { browserUrl: '' })).toBe('music');
    expect(resolveStageContentMode('agentic_task', { browserUrl: 'about:blank' })).toBe('music');
    expect(resolveStageContentMode('agentic_task', { agentFrameSrc: null })).toBe('music');
  });

  it('still shows the browser webview for a GENUINE browse', () => {
    expect(resolveStageContentMode('agentic_task', { browserModeActive: true })).toBe('browser');
    expect(resolveStageContentMode('agentic_task', { agentFrameSrc: 'data:image/png;...' })).toBe('browser');
    expect(resolveStageContentMode('agentic_task', { browserUrl: 'https://example.com' })).toBe('browser');
    // Explicit browser display mode (Qt bridge) is always the browser stage.
    expect(resolveStageContentMode('browser', { browserModeActive: true })).toBe('browser');
    expect(resolveStageContentMode('browser', {})).toBe('browser');
  });

  it('passes through the other stage modes unchanged', () => {
    expect(resolveStageContentMode('phone_call', {})).toBe('phone');
    expect(resolveStageContentMode('phone_tab', {})).toBe('phone');
    expect(resolveStageContentMode('chat', {})).toBe('chat');
    expect(resolveStageContentMode('now_playing', {})).toBe('music');
    expect(resolveStageContentMode('calendar', {})).toBe('music');
  });
});
