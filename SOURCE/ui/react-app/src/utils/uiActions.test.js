import { describe, it, expect, vi, afterEach } from 'vitest';
import { collectUiActions, dispatchUiActions } from './uiActions';

describe('uiActions', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('collects direct top-level ui_action envelopes', () => {
    expect(collectUiActions({
      ui_action: 'open_settings',
      tab: 'payment',
    })).toEqual([
      {
        action: 'open_settings',
        payload: { ui_action: 'open_settings', tab: 'payment' },
      },
    ]);
  });

  it('collects nested command_results tool envelopes', () => {
    const actions = collectUiActions({
      data: {
        ai_data: {
          command_results: [
            {
              tool: 'open_app_panel',
              data: {
                ui_action: 'open_payment_methods',
                tab: 'payment',
              },
            },
          ],
        },
      },
    });

    expect(actions).toHaveLength(1);
    expect(actions[0].action).toBe('open_payment_methods');
    expect(actions[0].payload.tab).toBe('payment');
  });

  it('collects MCP hub result envelopes with nested data.data ui_action', () => {
    const actions = collectUiActions({
      ai_data: {
        command_results: {
          routed: {
            message: 'Opening it now',
            data: {
              success: true,
              data: {
                ui_action: 'open_calendar',
                panel_id: 'calendar',
              },
            },
          },
        },
      },
    });

    expect(actions).toHaveLength(1);
    expect(actions[0].action).toBe('open_calendar');
  });

  it('collects legacy pairing_flow ui_action payloads', () => {
    const actions = collectUiActions({
      pairing_flow: {
        ui_action: 'open_rooms_add_speaker',
        rooms_modal_tab: 'add-speaker',
        target_room: 'kitchen',
      },
    });

    expect(actions[0].action).toBe('open_rooms_add_speaker');
    expect(actions[0].payload.target_room).toBe('kitchen');
  });

  it('dispatches custom viola ui-action events', () => {
    const listener = vi.fn();
    window.addEventListener('viola:ui-action', listener);

    dispatchUiActions({ ui_action: 'open_help' });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls[0][0].detail.action).toBe('open_help');
    window.removeEventListener('viola:ui-action', listener);
  });
});
