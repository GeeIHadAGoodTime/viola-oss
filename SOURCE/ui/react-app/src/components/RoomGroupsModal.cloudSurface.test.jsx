/**
 * #3553: the Rooms modal on the cloud SPA.
 *
 * Observed live on https://api.useviola.com/app: Menu -> Rooms opened a modal
 * that told the user to "Open this link on another device to add it as a room"
 * with no link and no QR code under it, while `GET /v1/rooms/groups`,
 * `GET /v1/network/local-address` and `GET /v1/network/pair/pending` all 404'd,
 * the last of them once every 2.5 seconds for as long as the modal stayed open.
 * Neither route group is served on cloud (backend/cloud_route_manifest.py:
 * "rooms" has no registration spec, "network" is LOCAL_ONLY) because rooms are
 * LAN speakers paired to a desktop hub.
 *
 * Same class as #4226: a desktop-only surface rendered on cloud over a backend
 * that is not there. The bar in both directions is the one that ticket set --
 * on cloud the user reads a DesktopUpsell and the SPA dials nothing at the
 * unserved groups; on desktop the modal still works exactly as before.
 *
 * Also pins the a11y shape: the modal is a hand-rolled overlay rather than the
 * shared Modal component, and it carried no dialog role, so assistive tech and
 * any [role=dialog] query saw a plain div where a modal was.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import RoomGroupsModal from './RoomGroupsModal';

const apiFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({ ok: true, data: { groups: [] } })));
const authFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({
  ok: true,
  status: 200,
  json: () => Promise.resolve({ ok: true, data: {} }),
})));

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: apiFetchMock,
  authFetch: authFetchMock,
}));

const requestedPaths = () => [
  ...apiFetchMock.mock.calls.map(([path]) => String(path)),
  ...authFetchMock.mock.calls.map(([path]) => String(path)),
];

const desktopOnlyPaths = () => requestedPaths().filter(
  (path) => path.startsWith('/v1/rooms') || path.startsWith('/api/v1/rooms') || path.startsWith('/v1/network'),
);

describe('Rooms modal on the cloud SPA (#3553)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiFetchMock.mockClear();
    authFetchMock.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  describe('cloud surface (no Qt bridge)', () => {
    beforeEach(() => {
      delete window.viola;
      window.history.replaceState({}, '', '/app');
    });

    it('explains that rooms live in the desktop app instead of showing pairing steps', async () => {
      render(<RoomGroupsModal isOpen onClose={vi.fn()} />);

      await screen.findByText(/Multi-room speaker sync runs over your local network/i);
      expect(screen.queryByText(/Open this link on another device/i)).toBeNull();
      expect(screen.queryByText(/same Wi-Fi network/i)).toBeNull();
      expect(screen.queryByLabelText(/Room name/i)).toBeNull();
    });

    it('dials none of the desktop-only room or network routes, even after the poll interval', async () => {
      render(<RoomGroupsModal isOpen onClose={vi.fn()} />);

      await screen.findByText(/Multi-room speaker sync runs over your local network/i);
      // The pending-pair poll re-armed itself every 2500ms; walk well past
      // several intervals so a surviving poll would show up here.
      await vi.advanceTimersByTimeAsync(12000);

      await waitFor(() => expect(desktopOnlyPaths()).toEqual([]));
    });
  });

  describe('desktop app (Qt bridge present)', () => {
    beforeEach(() => {
      window.viola = {};
      window.history.replaceState({}, '', '/');
    });

    it('still renders the room tabs and loads groups', async () => {
      render(<RoomGroupsModal isOpen onClose={vi.fn()} initialTab="groups" />);

      expect(screen.getByRole('button', { name: 'Add Room' })).toBeInTheDocument();
      expect(screen.queryByText(/Multi-room speaker sync runs over your local network/i)).toBeNull();
      await waitFor(() => expect(requestedPaths()).toContain('/v1/rooms/groups'));
    });
  });

  it('is discoverable as a modal dialog', () => {
    window.viola = {};
    render(<RoomGroupsModal isOpen onClose={vi.fn()} />);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Rooms');
  });
});
