import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import PTTButton from './PTTButton';
import ChatInput from '../stage/modes/chat/ChatInput';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const cssText = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'PTTButton.module.css'), 'utf8');

// jsdom has no PointerEvent, so fireEvent.pointerDown would drop pointerId,
// pointerType and button. A MouseEvent subclass carries them like Chromium.
class TestPointerEvent extends MouseEvent {
  constructor(type, init = {}) {
    super(type, init);
    this._pointerId = init.pointerId ?? 0;
    this._pointerType = init.pointerType ?? 'mouse';
  }

  get pointerId() { return this._pointerId; }

  get pointerType() { return this._pointerType; }
}

// Surface tablet mode: the mic button "slid under the finger" and push-to-talk
// was unreliable. These pin the press-and-hold contract: pointer capture, one
// start and one end per hold, and an end on cancel / lost capture.

const captured = new Map();
let originalPointerEvent;

beforeEach(() => {
  originalPointerEvent = window.PointerEvent;
  window.PointerEvent = TestPointerEvent;
  captured.clear();
  Element.prototype.setPointerCapture = vi.fn(function setPointerCapture(id) { captured.set(id, this); });
  Element.prototype.hasPointerCapture = vi.fn((id) => captured.has(id));
  Element.prototype.releasePointerCapture = vi.fn((id) => { captured.delete(id); });
});

afterEach(() => {
  if (originalPointerEvent) window.PointerEvent = originalPointerEvent;
  else delete window.PointerEvent;
  delete Element.prototype.setPointerCapture;
  delete Element.prototype.hasPointerCapture;
  delete Element.prototype.releasePointerCapture;
});

function renderButton(props = {}) {
  const onMouseDown = vi.fn();
  const onMouseUp = vi.fn();
  render(
    <PTTButton
      wakeStatus="listening"
      onMouseDown={onMouseDown}
      onMouseUp={onMouseUp}
      {...props}
    />,
  );
  return { button: screen.getByRole('button', { name: 'Push to talk' }), onMouseDown, onMouseUp };
}

const touch = (pointerId = 7) => ({ pointerId, pointerType: 'touch', button: 0 });

describe('PTTButton press-and-hold on touch', () => {
  it('starts on pointerdown, captures the pointer, and ends once on pointerup', () => {
    const { button, onMouseDown, onMouseUp } = renderButton();
    fireEvent.pointerDown(button, touch());
    expect(onMouseDown).toHaveBeenCalledTimes(1);
    expect(Element.prototype.setPointerCapture).toHaveBeenCalledWith(7);
    fireEvent.pointerUp(button, touch());
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('ends the hold when the browser cancels the gesture (scroll/drag stole it)', () => {
    const { button, onMouseDown, onMouseUp } = renderButton();
    fireEvent.pointerDown(button, touch());
    fireEvent.pointerCancel(button, touch());
    expect(onMouseDown).toHaveBeenCalledTimes(1);
    expect(onMouseUp).toHaveBeenCalledTimes(1);
    // The pointerup that may still arrive afterwards is not a second end.
    fireEvent.pointerUp(button, touch());
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('ends the hold when pointer capture is lost', () => {
    const { button, onMouseUp } = renderButton();
    fireEvent.pointerDown(button, touch());
    // A different pointer losing capture is not this hold.
    fireEvent.lostPointerCapture(button, touch(99));
    expect(onMouseUp).not.toHaveBeenCalled();
    fireEvent.lostPointerCapture(button, touch());
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('keeps holding when the finger drifts off the button', () => {
    const { button, onMouseUp } = renderButton();
    fireEvent.pointerDown(button, touch());
    fireEvent.pointerLeave(button, touch());
    fireEvent.mouseLeave(button);
    expect(onMouseUp).not.toHaveBeenCalled();
    fireEvent.pointerUp(button, touch());
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('does not start a second recording from the emulated mouse events after a tap', () => {
    const { button, onMouseDown, onMouseUp } = renderButton();
    fireEvent.pointerDown(button, touch());
    fireEvent.pointerUp(button, touch());
    fireEvent.mouseDown(button);
    fireEvent.mouseUp(button);
    fireEvent.touchStart(button);
    fireEvent.touchEnd(button);
    expect(onMouseDown).toHaveBeenCalledTimes(1);
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('ignores a second finger while one is holding', () => {
    const { button, onMouseDown, onMouseUp } = renderButton();
    fireEvent.pointerDown(button, touch(1));
    fireEvent.pointerDown(button, touch(2));
    fireEvent.pointerUp(button, touch(2));
    expect(onMouseDown).toHaveBeenCalledTimes(1);
    expect(onMouseUp).not.toHaveBeenCalled();
    fireEvent.pointerUp(button, touch(1));
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('ignores a right-click and blocks the long-press context menu', () => {
    const { button, onMouseDown } = renderButton();
    fireEvent.pointerDown(button, { pointerId: 1, pointerType: 'mouse', button: 2 });
    expect(onMouseDown).not.toHaveBeenCalled();
    const menu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    button.dispatchEvent(menu);
    expect(menu.defaultPrevented).toBe(true);
  });

  it('ends a hold that is still active when the button unmounts', () => {
    const onMouseUp = vi.fn();
    const { unmount } = render(<PTTButton wakeStatus="listening" onMouseUp={onMouseUp} />);
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Push to talk' }), touch());
    unmount();
    expect(onMouseUp).toHaveBeenCalledTimes(1);
  });

  it('does not start while disabled', () => {
    const { button, onMouseDown } = renderButton({ disabled: true });
    fireEvent.pointerDown(button, touch());
    expect(onMouseDown).not.toHaveBeenCalled();
  });

  it('sets touch-action: none so a press can never become a scroll or pan', () => {
    expect(cssText).toMatch(/touch-action:\s*none/);
    expect(cssText).not.toMatch(/touch-action:\s*manipulation/);
  });
});

describe('ChatInput push-to-talk on touch', () => {
  function renderChat() {
    const handlePTTStart = vi.fn();
    const handlePTTEnd = vi.fn();
    render(
      <ChatInput
        value=""
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        onAttachFiles={vi.fn()}
        handlePTTStart={handlePTTStart}
        handlePTTEnd={handlePTTEnd}
      />,
    );
    return { button: screen.getByRole('button', { name: 'Push to talk' }), handlePTTStart, handlePTTEnd };
  }

  it('ends on pointercancel and keeps holding through a drift off the button', () => {
    const { button, handlePTTStart, handlePTTEnd } = renderChat();
    fireEvent.pointerDown(button, touch());
    fireEvent.mouseLeave(button);
    expect(handlePTTEnd).not.toHaveBeenCalled();
    fireEvent.pointerCancel(button, touch());
    expect(handlePTTStart).toHaveBeenCalledTimes(1);
    expect(handlePTTEnd).toHaveBeenCalledTimes(1);
  });
});
