/**
 * Wake sensitivity control. `wake_sensitivity` is the wake score threshold:
 * higher is stricter. The old control showed the raw number as "sensitivity"
 * with a tooltip saying higher meant easier wake-ups, the opposite of what the
 * engine does. These tests pin the corrected direction and the migration rule:
 * a saved threshold keeps its meaning and is never rewritten unless the user
 * moves the control.
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '../../test/test-utils';
import WakeSensitivitySlider from './WakeSensitivitySlider';
import {
  WAKE_SENSITIVITY_LEVELS,
  clampWakeThreshold,
  positionToWakeThreshold,
  wakeThresholdToPosition,
} from '../../utils/wakeSensitivity';

const slider = () => screen.getByRole('slider', { name: 'Wake sensitivity' });

describe('wake sensitivity mapping', () => {
  it('keeps the 0.90 default threshold as the default (middle) position', () => {
    const position = wakeThresholdToPosition(0.9);
    expect(positionToWakeThreshold(position)).toBe(0.9);
    expect(WAKE_SENSITIVITY_LEVELS[position].label).toBe('Balanced (default)');
    expect(wakeThresholdToPosition(undefined)).toBe(position);
  });

  it('maps "More sensitive" to LOWER thresholds and "Fewer false wakes" to higher ones', () => {
    const thresholds = WAKE_SENSITIVITY_LEVELS.map((level) => level.threshold);
    // Left to right the threshold strictly falls.
    thresholds.slice(1).forEach((value, index) => expect(value).toBeLessThan(thresholds[index]));
    expect(thresholds[0]).toBe(0.97);
    expect(thresholds[thresholds.length - 1]).toBe(0.8);
  });

  it('migrates saved values without changing what they mean', () => {
    // Every level round-trips exactly.
    WAKE_SENSITIVITY_LEVELS.forEach((level, index) => {
      expect(wakeThresholdToPosition(level.threshold)).toBe(index);
      expect(positionToWakeThreshold(index)).toBe(level.threshold);
    });
    // A value saved by an older build or by voice snaps to the nearest position.
    expect(positionToWakeThreshold(wakeThresholdToPosition(0.92))).toBe(0.93);
    expect(positionToWakeThreshold(wakeThresholdToPosition(0.85))).toBe(0.84);
  });

  it('clamps values that guarantee constant false wakes or no wakes at all', () => {
    expect(clampWakeThreshold(0.5)).toBe(0.8);
    expect(clampWakeThreshold(0.05)).toBe(0.8);
    expect(clampWakeThreshold(1)).toBe(0.97);
    expect(clampWakeThreshold('0.93')).toBe(0.93);
    expect(clampWakeThreshold(null)).toBe(0.9);
    expect(clampWakeThreshold('')).toBe(0.9);
    expect(clampWakeThreshold(true)).toBe(0.9);
  });
});

describe('WakeSensitivitySlider', () => {
  it('labels the ends and shows the default position by name, not a raw number', () => {
    render(<WakeSensitivitySlider threshold={0.9} onChange={vi.fn()} />);
    expect(slider()).toHaveAttribute('aria-valuetext', 'Balanced (default)');
    expect(screen.getByText('Fewer false wakes')).toBeInTheDocument();
    expect(screen.getByText('More sensitive')).toBeInTheDocument();
    expect(screen.queryByText('0.9')).not.toBeInTheDocument();
  });

  it('moving toward More sensitive saves a lower threshold', () => {
    const onChange = vi.fn();
    render(<WakeSensitivitySlider threshold={0.9} onChange={onChange} />);
    fireEvent.keyDown(slider(), { key: 'ArrowRight' });
    expect(onChange).toHaveBeenCalledWith(0.87);
  });

  it('moving toward Fewer false wakes saves a higher (stricter) threshold', () => {
    const onChange = vi.fn();
    render(<WakeSensitivitySlider threshold={0.9} onChange={onChange} />);
    fireEvent.keyDown(slider(), { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenCalledWith(0.93);
  });

  it('cannot save a threshold outside 0.80-0.97', () => {
    const onChange = vi.fn();
    render(<WakeSensitivitySlider threshold={0.9} onChange={onChange} />);
    fireEvent.keyDown(slider(), { key: 'End' });
    fireEvent.keyDown(slider(), { key: 'Home' });
    expect(onChange.mock.calls.map(([value]) => value)).toEqual([0.8, 0.97]);
  });

  it('shows an out-of-range saved value at the nearest end without rewriting it', () => {
    const onChange = vi.fn();
    render(<WakeSensitivitySlider threshold={0.5} onChange={onChange} />);
    expect(slider()).toHaveAttribute('aria-valuetext', 'Most sensitive');
    expect(onChange).not.toHaveBeenCalled();
  });
});
