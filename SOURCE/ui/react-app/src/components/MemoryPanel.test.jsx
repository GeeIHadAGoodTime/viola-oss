/**
 * #1182: the browser SPA's Memory panel used to call the desktop-local
 * D-layout file routes (/api/memory/viola, /api/memory/entries -- desktop
 * filesystem only) and, once #1064 caught that dead fetch, degraded to a
 * DesktopUpsell on the cloud SPA instead of ever wiring the panel onto the
 * cloud-native /v1/memories/* API that was already live. This suite is the
 * negative case for that shape: it fails if the cloud-surface Memory tab
 * stops calling /v1/memories, starts calling the desktop file routes again,
 * or falls back to the DesktopUpsell -- and it proves real create/list/
 * delete + an honest consent-required empty state, not just that the
 * component mounts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../test/test-utils';
import MemoryPanel from './MemoryPanel';

const authFetchMock = vi.fn();

vi.mock('../hooks/useViolaApi', () => ({
  authFetch: (...args) => authFetchMock(...args),
}));

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  };
}

describe('MemoryPanel -- cloud SPA Memory tab reads/writes /v1/memories (#1182)', () => {
  let store;
  let nextId;

  beforeEach(() => {
    // No window.viola bridge + no spoke param => isCloudSurface() is true
    // (see utils/featureSurface.test.js for the same convention).
    delete window.viola;
    window.history.replaceState({}, '', '/app');

    store = {
      1: {
        id: 1,
        content: 'Loves jazz on weekends',
        category: 'preference',
        critical: false,
        verified: false,
        created_at: '2026-07-01T00:00:00Z',
        updated_at: '2026-07-01T00:00:00Z',
      },
    };
    nextId = 2;

    authFetchMock.mockImplementation(async (path, options = {}) => {
      const method = options.method || 'GET';

      if (path.startsWith('/v1/memories/quarantine')) {
        return jsonResponse(200, { ok: true, error: null, data: { rows: [] } });
      }
      if (path.startsWith('/v1/memories?') && method === 'GET') {
        return jsonResponse(200, { ok: true, error: null, data: { rows: Object.values(store) } });
      }
      if (path === '/v1/memories' && method === 'POST') {
        const body = JSON.parse(options.body);
        const id = nextId++;
        store[id] = {
          id,
          content: body.content,
          category: body.category,
          critical: Boolean(body.critical),
          verified: false,
          created_at: '2026-07-16T00:00:00Z',
          updated_at: '2026-07-16T00:00:00Z',
        };
        return jsonResponse(201, { ok: true, error: null, data: { memory: store[id] } });
      }
      const idMatch = path.match(/^\/v1\/memories\/(\d+)$/);
      if (idMatch && method === 'DELETE') {
        const id = Number(idMatch[1]);
        delete store[id];
        return jsonResponse(200, { ok: true, error: null, data: { deleted: true, memory_id: id } });
      }
      if (idMatch && method === 'PATCH') {
        const id = Number(idMatch[1]);
        const body = JSON.parse(options.body);
        store[id] = { ...store[id], ...body };
        return jsonResponse(200, { ok: true, error: null, data: { memory: store[id] } });
      }
      throw new Error(`Unhandled authFetch call in test: ${method} ${path}`);
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
    delete window.viola;
  });

  it('renders a real cloud memory via /v1/memories instead of a DesktopUpsell', async () => {
    render(<MemoryPanel isOpen onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Loves jazz on weekends')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Available in the desktop app/i)).not.toBeInTheDocument();
    // No 'viola' tab on the cloud SPA (no VIOLA.md filesystem there).
    expect(screen.queryByRole('button', { name: 'Viola' })).not.toBeInTheDocument();

    const calledPaths = authFetchMock.mock.calls.map(([path]) => path);
    expect(calledPaths.some((p) => p.startsWith('/v1/memories?'))).toBe(true);
    expect(calledPaths.some((p) => p.startsWith('/api/memory/'))).toBe(false);
  });

  it('adds a memory through the UI and it persists via a real POST /v1/memories', async () => {
    render(<MemoryPanel isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Loves jazz on weekends')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('cloud-memory-new-content'), {
      target: { value: 'Allergic to peanuts' },
    });
    fireEvent.click(screen.getByTestId('cloud-memory-add'));

    await waitFor(() => expect(screen.getByText('Allergic to peanuts')).toBeInTheDocument());

    const postCall = authFetchMock.mock.calls.find(
      ([path, options]) => path === '/v1/memories' && options?.method === 'POST'
    );
    expect(postCall).toBeDefined();
    expect(JSON.parse(postCall[1].body)).toMatchObject({ content: 'Allergic to peanuts' });
  });

  it('deletes a memory via DELETE /v1/memories/{id} and it disappears from the list', async () => {
    render(<MemoryPanel isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Loves jazz on weekends')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(screen.queryByText('Loves jazz on weekends')).not.toBeInTheDocument());
    expect(
      authFetchMock.mock.calls.some(([path, options]) => path === '/v1/memories/1' && options?.method === 'DELETE')
    ).toBe(true);
  });

  it('shows an honest consent prompt instead of crashing when cloud sync consent is not granted', async () => {
    authFetchMock.mockImplementation(async () =>
      jsonResponse(403, {
        ok: false,
        error: { code: 'consent_required', message: 'Enable cloud sync in Settings to use this feature.' },
        data: null,
      })
    );

    render(<MemoryPanel isOpen onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText(/Cloud Sync/i)).toBeInTheDocument());
    expect(screen.queryByText(/Available in the desktop app/i)).not.toBeInTheDocument();
  });
});
