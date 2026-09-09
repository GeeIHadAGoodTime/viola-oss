import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '../test/test-utils';
import CalendarView, { computeLocalCalendarEndTime } from './CalendarView';
import { apiFetch } from '../hooks/useViolaApi';

const calendarHarness = vi.hoisted(() => ({
  useCalendarEvents: vi.fn(),
}));

vi.mock('../hooks/useCalendarEvents', () => ({
  useCalendarEvents: calendarHarness.useCalendarEvents,
}));

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(() => Promise.resolve({})),
}));

const theme = {
  colors: {
    accent: '#C89B3C',
    accentGlow: 'rgba(200, 155, 60, 0.3)',
    bgCard: '#0d0d0d',
    bgElevated: '#1a1a1a',
    borderHover: 'rgba(255,255,255,0.12)',
    borderLight: 'rgba(255,255,255,0.06)',
    glassActive: 'rgba(255,255,255,0.14)',
    glassBase: 'rgba(255,255,255,0.07)',
    glassHover: 'rgba(255,255,255,0.10)',
    overlay: 'rgba(0,0,0,0.85)',
    shadowDeep: 'rgba(0,0,0,0.8)',
    statusRed: '#ef4444',
    textMuted: 'rgba(255,255,255,0.45)',
    textPrimary: 'rgba(255,255,255,0.85)',
  },
};

function mockCalendarEvents() {
  calendarHarness.useCalendarEvents.mockReturnValue({
    viewDate: new Date('2026-05-01T12:00:00'),
    eventsByDate: {
      '2026-05-12': [{
        event_id: 'event-1',
        title: 'Planning review',
        time: '10:00 AM',
      }],
    },
    loading: false,
    error: null,
    calendarStatus: 'connected',
    navigateMonth: vi.fn(),
    goToToday: vi.fn(),
    refetch: vi.fn(),
  });
}

describe('CalendarView', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-11T12:00:00'));
    mockCalendarEvents();
    // Calendar links to the user's Google/Microsoft account on the DESKTOP app
    // (window.viola bridge present). The cloud web build hides it behind a
    // desktop upsell — covered separately below.
    window.viola = {};
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    delete window.viola;
  });

  it('updates compact events when a day is clicked without expanding the calendar', () => {
    render(<CalendarView theme={theme} compact />);

    fireEvent.click(screen.getByTestId('calendar-day-2026-05-12'));

    expect(screen.getByTestId('calendar-compact')).toBeInTheDocument();
    expect(screen.getByTestId('calendar-compact-events')).toHaveTextContent('Planning review');
    expect(screen.queryByText('May 2026')).not.toBeInTheDocument();
  });

  it('opens compact expansion in a modal and closes it with Escape', () => {
    render(<CalendarView theme={theme} compact />);

    fireEvent.click(screen.getByTestId('calendar-expand-button'));

    expect(screen.getByTestId('calendar-compact')).toBeInTheDocument();
    expect(screen.getByTestId('calendar-expanded-modal')).toBeInTheDocument();
    expect(screen.getByTestId('calendar-expanded-content')).toHaveTextContent('May 2026');

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByTestId('calendar-expanded-modal')).not.toBeInTheDocument();
  });

  it('computes edited event end time as local wall-clock time', () => {
    expect(computeLocalCalendarEndTime('2026-05-12T09:00:00')).toBe('2026-05-12T10:00:00');
  });

  // Regression for #2775: create-event omitted end_time while edit included
  // it, giving newly-created events asymmetric (backend-defaulted) duration
  // semantics compared to edited ones.
  it('sends end_time on event creation, matching the edit path', async () => {
    render(<CalendarView theme={theme} compact />);

    // The add-event form only renders in the expanded day-detail view.
    fireEvent.click(screen.getByTestId('calendar-expand-button'));
    fireEvent.click(screen.getAllByTestId('calendar-day-2026-05-12')[0]);
    fireEvent.click(screen.getByText('+ Add event'));
    fireEvent.change(screen.getByPlaceholderText('Event title'), {
      target: { value: 'Team sync' },
    });
    fireEvent.change(screen.getByDisplayValue(/2026-05-12T09:00/), {
      target: { value: '2026-05-12T14:00' },
    });

    await fireEvent.click(screen.getByText('Save'));

    expect(apiFetch).toHaveBeenCalledWith('/v1/calendar/events', expect.objectContaining({
      method: 'POST',
      body: expect.stringContaining('"end_time":"2026-05-12T15:00:00"'),
    }));
  });
});

describe('CalendarView on the cloud web build', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-11T12:00:00'));
    mockCalendarEvents();
    // No window.viola bridge -> cloud SPA surface.
    delete window.viola;
    window.history.replaceState({}, '', '/app');
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
  });

  // Regression for the 2026-07-06 "calendar doesn't work in the browser" bug:
  // featureSurface.js:47 listed `calendar` in DESKTOP_ONLY_FEATURES from
  // before the self-hosted CalDAV work shipped cloud calendar CRUD
  // (backend/cloud_route_manifest.py "calendar" group is CLOUD_NATIVE,
  // tenant-safety reviewed, registered), so this component rendered
  // DesktopUpsell instead of the working CalendarViewInner CRUD grid for
  // every cloud user. Calendar must render live on the cloud SPA exactly
  // like it does on desktop/spoke.
  it('renders the live calendar CRUD grid against the cloud API path, not the desktop upsell', () => {
    render(<CalendarView theme={theme} compact />);

    expect(screen.getByTestId('calendar-compact')).toBeInTheDocument();
    expect(screen.queryByText('Available in the desktop app')).not.toBeInTheDocument();
    expect(calendarHarness.useCalendarEvents).toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('calendar-day-2026-05-12'));

    expect(screen.getByTestId('calendar-compact-events')).toHaveTextContent('Planning review');
  });

  it('expands the cloud calendar the same way as desktop', () => {
    render(<CalendarView theme={theme} compact />);

    fireEvent.click(screen.getByTestId('calendar-expand-button'));

    expect(screen.getByTestId('calendar-expanded-modal')).toBeInTheDocument();
    expect(screen.getByTestId('calendar-expanded-content')).toHaveTextContent('May 2026');
  });
});

describe('CalendarView on a multiroom spoke', () => {
  beforeEach(() => {
    mockCalendarEvents();
    delete window.viola;
    window.history.replaceState({}, '', '/?room=kitchen&spoke_token=qr-token');
  });

  afterEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
  });

  it('renders the live hub calendar', () => {
    render(<CalendarView theme={theme} compact isSpoke />);

    expect(screen.getByTestId('calendar-compact')).toBeInTheDocument();
    expect(screen.queryByText('Available in the desktop app')).not.toBeInTheDocument();
    expect(screen.queryByText(/Google or Microsoft account/i)).not.toBeInTheDocument();
    expect(calendarHarness.useCalendarEvents).toHaveBeenCalled();
  });
});
