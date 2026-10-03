import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render } from '../../test/test-utils';
import Slider from './Slider';

describe('Slider', () => {
  let originalGetBoundingClientRect;

  beforeEach(() => {
    originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
    // jsdom never lays out real geometry — stub a fixed track rect so drag
    // math (which reads trackRef.current.getBoundingClientRect()) is
    // deterministic across the test.
    HTMLElement.prototype.getBoundingClientRect = function stubbedRect() {
      return { left: 0, right: 100, top: 0, bottom: 44, width: 100, height: 44 };
    };
  });

  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect;
  });

  // Regression for #2775: Math.round(newValue / step) * step had no final
  // clamp, so a step that doesn't evenly divide (max - min) could push the
  // emitted value past either end — e.g. max=10, step=4 rounds the far-right
  // edge (value 10) up to 12. Harmless for the common 0-100/step-1 sliders,
  // real for any max/step combo where (max - min) isn't a step multiple.
  it('never emits a value above max when step does not evenly divide the range', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Slider value={0} onChange={onChange} min={0} max={10} step={4} label="Test" />,
    );

    const track = container.querySelector('[data-hold-interactive]');
    fireEvent.mouseDown(track, { clientX: 100 }); // far-right edge

    expect(onChange).toHaveBeenCalledTimes(1);
    const [emitted] = onChange.mock.calls[0];
    expect(emitted).toBeLessThanOrEqual(10);
    expect(emitted).toBeGreaterThanOrEqual(0);
  });

  it('never emits a value below min at the far-left edge', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Slider value={10} onChange={onChange} min={0} max={10} step={4} label="Test" />,
    );

    const track = container.querySelector('[data-hold-interactive]');
    fireEvent.mouseDown(track, { clientX: -50 }); // past the left edge

    expect(onChange).toHaveBeenCalledTimes(1);
    const [emitted] = onChange.mock.calls[0];
    expect(emitted).toBeGreaterThanOrEqual(0);
    expect(emitted).toBeLessThanOrEqual(10);
  });
  it('exposes its name, current value and bounds to keyboard and assistive technology', () => {
    const {getByRole}=render(<Slider value={40} onChange={vi.fn()} min={0} max={100} label="Assistant Volume" />);
    const slider=getByRole('slider',{name:'Assistant Volume'});
    expect(slider.getAttribute('tabindex')).toBe('0');
    expect(slider.getAttribute('aria-valuenow')).toBe('40');
    expect(slider.getAttribute('aria-valuemin')).toBe('0');
    expect(slider.getAttribute('aria-valuemax')).toBe('100');
  });

  it.each([['ArrowRight',41],['ArrowUp',41],['ArrowLeft',39],['ArrowDown',39],['Home',0],['End',100],['PageUp',50],['PageDown',30]])('supports keyboard %s without dragging', (key, expected) => {
    const onChange=vi.fn();
    const {getByRole}=render(<Slider value={40} onChange={onChange} label="Volume" />);
    fireEvent.keyDown(getByRole('slider',{name:'Volume'}),{key});
    expect(onChange).toHaveBeenCalledWith(expected);
  });

});
