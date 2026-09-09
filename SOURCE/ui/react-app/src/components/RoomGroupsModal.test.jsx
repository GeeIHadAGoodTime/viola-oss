import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { GroupCard, RoomMemberItem } from './RoomGroupsModal';

// #3003: both sliders in the Rooms modal — a room's "Volume Offset" and a
// group's master volume — were controlled inputs bound straight to
// server-fetched state (`member.volume_offset`, `group.master_volume` via
// useRoomGroups), with no local value and no throttle on the write. So the
// thumb could not move until a POST came back, every input tick fired its own
// POST, and a response landing mid-drag re-pinned the thumb to a value the
// user had already dragged past. Same class as #2772 in
// player/VolumeControl.jsx, which landed first.
//
// These drive the real slider elements the way a mouse drag does and assert on
// what the user sees plus how many writes actually leave — not on internals.

function buildMember(overrides = {}) {
  return {
    room_id: 'kitchen',
    room_name: 'Kitchen',
    volume_offset: 10,
    is_muted: false,
    ...overrides,
  };
}

function buildGroup(overrides = {}) {
  return {
    group_id: 'g1',
    group_name: 'Downstairs',
    master_volume: 60,
    members: [buildMember()],
    ...overrides,
  };
}

describe('RoomMemberItem volume offset (#3003)', () => {
  const offsetSlider = () => screen.getByRole('slider', { name: 'Kitchen volume offset' });

  function renderMember(props = {}) {
    const onVolumeChange = props.onVolumeChange || vi.fn();
    const { rerender } = render(
      <RoomMemberItem
        member={buildMember({ volume_offset: props.volumeOffset ?? 10 })}
        onVolumeChange={onVolumeChange}
        onMuteToggle={vi.fn()}
      />
    );
    const serverSays = (volume_offset) => rerender(
      <RoomMemberItem
        member={buildMember({ volume_offset })}
        onVolumeChange={onVolumeChange}
        onMuteToggle={vi.fn()}
      />
    );
    return { onVolumeChange, serverSays };
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('tracks every drag tick immediately, without waiting on a round trip', () => {
    renderMember();
    expect(offsetSlider()).toHaveValue('10');

    fireEvent.change(offsetSlider(), { target: { value: '20' } });
    expect(offsetSlider()).toHaveValue('20');

    fireEvent.change(offsetSlider(), { target: { value: '-5' } });
    expect(offsetSlider()).toHaveValue('-5');
  });

  it('collapses a fast drag into one write carrying the value the user let go on', () => {
    const { onVolumeChange } = renderMember();

    for (const tick of [12, 15, 18, 22, 25, 28, 30]) {
      fireEvent.change(offsetSlider(), { target: { value: String(tick) } });
    }

    expect(offsetSlider()).toHaveValue('30');
    expect(onVolumeChange).not.toHaveBeenCalled();

    vi.advanceTimersByTime(50);

    expect(onVolumeChange).toHaveBeenCalledTimes(1);
    expect(onVolumeChange).toHaveBeenCalledWith(30);
  });

  it('writes on release without waiting out the debounce, and only once', () => {
    const { onVolumeChange } = renderMember();

    fireEvent.change(offsetSlider(), { target: { value: '35' } });
    fireEvent.change(offsetSlider(), { target: { value: '40' } });
    fireEvent.pointerUp(offsetSlider());

    expect(onVolumeChange).toHaveBeenCalledTimes(1);
    expect(onVolumeChange).toHaveBeenCalledWith(40);

    vi.advanceTimersByTime(500);
    expect(onVolumeChange).toHaveBeenCalledTimes(1);
  });

  it('ignores the echo of its own write', () => {
    const { serverSays } = renderMember();

    fireEvent.change(offsetSlider(), { target: { value: '20' } });
    fireEvent.pointerUp(offsetSlider());

    serverSays(20);
    expect(offsetSlider()).toHaveValue('20');
  });

  it('cannot be snapped back by a late response to a write the user has already superseded', () => {
    const { onVolumeChange, serverSays } = renderMember();

    // First write goes out...
    fireEvent.change(offsetSlider(), { target: { value: '20' } });
    vi.advanceTimersByTime(50);
    expect(onVolumeChange).toHaveBeenLastCalledWith(20);

    // ...the user keeps going and a second write supersedes it.
    fireEvent.change(offsetSlider(), { target: { value: '-10' } });
    vi.advanceTimersByTime(50);
    expect(onVolumeChange).toHaveBeenLastCalledWith(-10);

    // Now the FIRST write's response finally lands, out of order, carrying the
    // stale 20. Matching a value we did send is exactly why a value-only echo
    // check is not enough here.
    serverSays(20);
    expect(offsetSlider()).toHaveValue('-10');
  });

  it('adopts a genuine external change (voice command, another client) while idle', () => {
    const { serverSays } = renderMember();
    expect(offsetSlider()).toHaveValue('10');

    serverSays(-25);
    expect(offsetSlider()).toHaveValue('-25');
  });

  it('goes back to following the server once the drag has settled', () => {
    const { serverSays } = renderMember();

    fireEvent.change(offsetSlider(), { target: { value: '30' } });
    fireEvent.pointerUp(offsetSlider());
    serverSays(30);

    // Well past the settle window: the user is no longer touching it, so a
    // change from elsewhere must come through.
    vi.advanceTimersByTime(5000);
    serverSays(-40);
    expect(offsetSlider()).toHaveValue('-40');
  });
});

describe('GroupCard master volume (#3003)', () => {
  const masterSlider = () => screen.getByRole('slider', { name: 'Downstairs volume' });

  function renderGroup(props = {}) {
    const onMasterVolumeChange = props.onMasterVolumeChange || vi.fn();
    const rest = {
      onDelete: vi.fn(),
      onRoomVolumeChange: vi.fn(),
      onRoomMuteToggle: vi.fn(),
    };
    const { rerender } = render(
      <GroupCard
        group={buildGroup({ master_volume: props.masterVolume ?? 60 })}
        onMasterVolumeChange={onMasterVolumeChange}
        {...rest}
      />
    );
    const serverSays = (master_volume) => rerender(
      <GroupCard
        group={buildGroup({ master_volume })}
        onMasterVolumeChange={onMasterVolumeChange}
        {...rest}
      />
    );
    return { onMasterVolumeChange, serverSays };
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('tracks every drag tick immediately, and the percentage beside it agrees', () => {
    renderGroup();
    expect(masterSlider()).toHaveValue('60');

    fireEvent.change(masterSlider(), { target: { value: '40' } });
    expect(masterSlider()).toHaveValue('40');
    expect(screen.getByText('40%')).toBeInTheDocument();

    fireEvent.change(masterSlider(), { target: { value: '15' } });
    expect(masterSlider()).toHaveValue('15');
    expect(screen.getByText('15%')).toBeInTheDocument();
  });

  it('collapses a fast drag into one write carrying the value the user let go on', () => {
    const { onMasterVolumeChange } = renderGroup();

    for (const tick of [58, 54, 50, 45, 40, 35, 30]) {
      fireEvent.change(masterSlider(), { target: { value: String(tick) } });
    }

    expect(masterSlider()).toHaveValue('30');
    expect(onMasterVolumeChange).not.toHaveBeenCalled();

    vi.advanceTimersByTime(50);

    expect(onMasterVolumeChange).toHaveBeenCalledTimes(1);
    expect(onMasterVolumeChange).toHaveBeenCalledWith(30);
  });

  it('cannot be snapped back by a late response to a write the user has already superseded', () => {
    const { onMasterVolumeChange, serverSays } = renderGroup();

    fireEvent.change(masterSlider(), { target: { value: '50' } });
    vi.advanceTimersByTime(50);
    expect(onMasterVolumeChange).toHaveBeenLastCalledWith(50);

    fireEvent.change(masterSlider(), { target: { value: '20' } });
    vi.advanceTimersByTime(50);
    expect(onMasterVolumeChange).toHaveBeenLastCalledWith(20);

    serverSays(50);
    expect(masterSlider()).toHaveValue('20');
    expect(screen.getByText('20%')).toBeInTheDocument();
  });

  it('adopts a genuine external change while idle', () => {
    const { serverSays } = renderGroup();
    expect(masterSlider()).toHaveValue('60');

    serverSays(22);
    expect(masterSlider()).toHaveValue('22');
    expect(screen.getByText('22%')).toBeInTheDocument();
  });

  it('can be left at zero', () => {
    // A group silenced all the way down reads back as a falsy 0, which used to
    // render as the 50 default — so zero was a value the slider could show but
    // never keep.
    const { serverSays } = renderGroup({ masterVolume: 0 });
    expect(masterSlider()).toHaveValue('0');
    expect(screen.getByText('0%')).toBeInTheDocument();

    fireEvent.change(masterSlider(), { target: { value: '0' } });
    fireEvent.pointerUp(masterSlider());
    serverSays(0);

    expect(masterSlider()).toHaveValue('0');
    expect(screen.getByText('0%')).toBeInTheDocument();
  });

  it('falls back to 50 only when the group has no master volume at all', () => {
    render(
      <GroupCard
        group={{ group_id: 'g1', group_name: 'Downstairs', members: [] }}
        onDelete={vi.fn()}
        onMasterVolumeChange={vi.fn()}
        onRoomVolumeChange={vi.fn()}
        onRoomMuteToggle={vi.fn()}
      />
    );
    expect(masterSlider()).toHaveValue('50');
  });

  it('does not open or shut the card when the drag ends on the slider', () => {
    renderGroup();
    // Collapsed to start with: the member rows only render when expanded.
    expect(screen.queryByText('Room Volumes')).toBeNull();

    fireEvent.change(masterSlider(), { target: { value: '35' } });
    fireEvent.pointerUp(masterSlider());
    fireEvent.click(masterSlider());

    expect(screen.queryByText('Room Volumes')).toBeNull();
    expect(masterSlider()).toHaveValue('35');
  });
});
