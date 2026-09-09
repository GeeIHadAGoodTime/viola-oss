import { describe, expect, it, vi, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../../../../test/test-utils';
import BrowserMode from './BrowserMode';

// A 900x520 surface rect so click ratios are deterministic.
function stubSurfaceRect(surface) {
  surface.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    width: 900,
    height: 520,
    top: 0,
    left: 0,
    right: 900,
    bottom: 520,
    toJSON: () => {},
  });
}

function findRegisteredCommand(registerCommands, label) {
  for (let index = registerCommands.mock.calls.length - 1; index >= 0; index -= 1) {
    const commands = registerCommands.mock.calls[index][1] || [];
    const command = commands.find((item) => item.label === label);
    if (command) return command;
  }
  return null;
}

describe('BrowserMode', () => {
  afterEach(() => {
    delete window.viola;
  });

  it('publishes only the browser surface bounds to the native webview', async () => {
    const setBrowserOverlayBounds = vi.fn();
    window.viola = { setBrowserOverlayBounds };

    render(<BrowserMode browserUrl="https://example.com" />);

    const surface = screen.getByTestId('browser-surface');
    surface.getBoundingClientRect = () => ({
      x: 24,
      y: 96,
      width: 900,
      height: 520,
      top: 96,
      left: 24,
      right: 924,
      bottom: 616,
      toJSON: () => {},
    });

    act(() => {
      window.dispatchEvent(new Event('resize'));
    });

    await waitFor(() => {
      expect(setBrowserOverlayBounds).toHaveBeenLastCalledWith(24, 96, 900, 520);
    });
    expect(setBrowserOverlayBounds).not.toHaveBeenCalledWith(24, 48, 900, 568);
  });

  it('registers active browser takeover commands', async () => {
    const registerCommands = vi.fn(() => vi.fn());
    const wsSend = vi.fn();
    const onTakeoverChange = vi.fn();

    render(
      <BrowserMode
        browserUrl="https://example.com"
        commandScopeActive
        commandRegistry={{ registerCommands }}
        wsSend={wsSend}
        onTakeoverChange={onTakeoverChange}
      />
    );

    await waitFor(() => {
      expect(findRegisteredCommand(registerCommands, 'Take over browser')).toBeTruthy();
    });

    const takeOver = findRegisteredCommand(registerCommands, 'Take over browser');
    act(() => {
      takeOver.perform();
    });

    expect(wsSend).toHaveBeenCalledWith({ action: 'agent_takeover', payload: {} });
    expect(onTakeoverChange).toHaveBeenCalledWith(true);
  });

  it('registers handback and stop commands for controlled active tasks', async () => {
    const registerCommands = vi.fn(() => vi.fn());
    const wsSend = vi.fn();
    const onTakeoverChange = vi.fn();

    render(
      <BrowserMode
        browserUrl="https://example.com"
        agentBusy
        takeoverActive
        commandScopeActive
        commandRegistry={{ registerCommands }}
        wsSend={wsSend}
        onTakeoverChange={onTakeoverChange}
      />
    );

    await waitFor(() => {
      expect(findRegisteredCommand(registerCommands, 'Hand back to agent')).toBeTruthy();
      expect(findRegisteredCommand(registerCommands, 'Stop agent')).toBeTruthy();
    });

    act(() => {
      findRegisteredCommand(registerCommands, 'Hand back to agent').perform();
      findRegisteredCommand(registerCommands, 'Stop agent').perform();
    });

    expect(wsSend).toHaveBeenCalledWith({ action: 'agent_continue', payload: {} });
    expect(onTakeoverChange).toHaveBeenCalledWith(false);
    expect(wsSend).toHaveBeenCalledWith({ action: 'agent_cancel', payload: {} });
  });

  // ---- Cloud-web client streamed-frame path (streamFrames prop) ---- //

  it('renders the streamed agent frame for a cloud web client', () => {
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
      />
    );

    const frame = screen.getByAltText('Agent browser view');
    expect(frame).toBeInTheDocument();
    expect(frame.getAttribute('src')).toBe('blob:agent-frame');
  });

  it('shows a waiting state for a cloud web client before the first frame', () => {
    render(<BrowserMode browserUrl="https://example.com" streamFrames />);
    expect(screen.getByText('Waiting for browser view')).toBeInTheDocument();
  });

  it('relays a cloud-web click as a resolution-independent ratio over agent_browser_input', () => {
    const wsSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
      />
    );

    const surface = screen.getByTestId('browser-surface');
    stubSurfaceRect(surface);

    fireEvent.click(surface, { clientX: 450, clientY: 130 });

    expect(wsSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: {
        type: 'mouse_click',
        x_ratio: 0.5,
        y_ratio: 0.25,
        width: 900,
        height: 520,
      },
    });
  });

  it('relays a printable keystroke as text_input over agent_browser_input', () => {
    const wsSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
      />
    );

    const surface = screen.getByTestId('browser-surface');
    fireEvent.keyDown(surface, { key: 'a' });

    expect(wsSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: { type: 'text_input', text: 'a' },
    });
  });

  it('relays a named key as key_press over agent_browser_input', () => {
    const wsSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
      />
    );

    const surface = screen.getByTestId('browser-surface');
    fireEvent.keyDown(surface, { key: 'Enter' });

    expect(wsSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: {
        type: 'key_press',
        key: 'Enter',
        ctrl: false,
        meta: false,
        alt: false,
        shift: false,
      },
    });
  });

  it('relays a modified shortcut as key_press with modifier flags', () => {
    const wsSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
      />
    );

    const surface = screen.getByTestId('browser-surface');
    fireEvent.keyDown(surface, { key: 'a', ctrlKey: true });

    expect(wsSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: {
        type: 'key_press',
        key: 'a',
        ctrl: true,
        meta: false,
        alt: false,
        shift: false,
      },
    });
  });

  it('does NOT render streamed frames or relay input on the desktop app (no streamFrames)', () => {
    const wsSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
      />
    );

    expect(screen.queryByAltText('Agent browser view')).not.toBeInTheDocument();
    expect(screen.queryByText('Waiting for browser view')).not.toBeInTheDocument();

    const surface = screen.getByTestId('browser-surface');
    stubSurfaceRect(surface);
    fireEvent.click(surface, { clientX: 450, clientY: 130 });
    fireEvent.keyDown(surface, { key: 'Enter' });

    // The desktop hub uses a native embedded webview — no WS input relay.
    expect(wsSend).not.toHaveBeenCalled();
  });

  // ---- Dedicated input sender split (cloud /ws/agent-browser stream) ---- //

  it('relays agent_browser_input through inputSend, NOT the control wsSend', () => {
    const wsSend = vi.fn();
    const inputSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
        inputSend={inputSend}
      />
    );

    const surface = screen.getByTestId('browser-surface');
    stubSurfaceRect(surface);

    fireEvent.click(surface, { clientX: 450, clientY: 130 });
    fireEvent.keyDown(surface, { key: 'a' });
    fireEvent.keyDown(surface, { key: 'Enter' });

    // All three input events went through the dedicated stream sender.
    expect(inputSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: { type: 'mouse_click', x_ratio: 0.5, y_ratio: 0.25, width: 900, height: 520 },
    });
    expect(inputSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: { type: 'text_input', text: 'a' },
    });
    expect(inputSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: {
        type: 'key_press', key: 'Enter', ctrl: false, meta: false, alt: false, shift: false,
      },
    });
    // The control socket received NONE of the input events.
    expect(wsSend).not.toHaveBeenCalled();
  });

  it('keeps takeover/cancel control actions on wsSend even when inputSend is set', async () => {
    const wsSend = vi.fn();
    const inputSend = vi.fn();
    const onTakeoverChange = vi.fn();
    const registerCommands = vi.fn(() => vi.fn());

    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentBusy
        agentFrameSrc="blob:agent-frame"
        commandScopeActive
        commandRegistry={{ registerCommands }}
        wsSend={wsSend}
        inputSend={inputSend}
        onTakeoverChange={onTakeoverChange}
      />
    );

    await waitFor(() => {
      expect(findRegisteredCommand(registerCommands, 'Take over browser')).toBeTruthy();
    });

    act(() => {
      findRegisteredCommand(registerCommands, 'Take over browser').perform();
      findRegisteredCommand(registerCommands, 'Stop agent').perform();
    });

    // Control actions stay on the shared /ws/events sender.
    expect(wsSend).toHaveBeenCalledWith({ action: 'agent_takeover', payload: {} });
    expect(wsSend).toHaveBeenCalledWith({ action: 'agent_cancel', payload: {} });
    // ...and never leaked onto the input stream sender.
    expect(inputSend).not.toHaveBeenCalled();
  });

  it('falls back to wsSend for input when inputSend is not provided', () => {
    const wsSend = vi.fn();
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        agentFrameSrc="blob:agent-frame"
        wsSend={wsSend}
      />
    );

    const surface = screen.getByTestId('browser-surface');
    stubSurfaceRect(surface);
    fireEvent.click(surface, { clientX: 450, clientY: 130 });

    expect(wsSend).toHaveBeenCalledWith({
      action: 'agent_browser_input',
      payload: { type: 'mouse_click', x_ratio: 0.5, y_ratio: 0.25, width: 900, height: 520 },
    });
  });

  // ---- Stream status / error surfaced in the stage waiting area ---- //

  it('shows a no_agent_browser stream error instead of a permanent spinner', () => {
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        streamStatus="error"
        streamError={{ code: 'no_agent_browser', message: 'Viola is not browsing right now.' }}
      />
    );

    expect(screen.getByText('Viola is not browsing right now.')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for browser view')).not.toBeInTheDocument();
  });

  it('shows a plan_tier_required stream error in the stage', () => {
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        streamStatus="error"
        streamError={{ code: 'plan_tier_required', message: '' }}
      />
    );

    expect(screen.getByText('Live browsing is available on a paid plan.')).toBeInTheDocument();
  });

  it('shows a connecting message while the dedicated stream is connecting', () => {
    render(
      <BrowserMode
        browserUrl="https://example.com"
        streamFrames
        streamStatus="connecting"
      />
    );

    expect(screen.getByText('Connecting to browser view')).toBeInTheDocument();
  });
});
