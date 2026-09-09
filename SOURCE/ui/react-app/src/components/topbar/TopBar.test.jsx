import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import TopBar from './TopBar';

const handlers = {
  setWeatherRetryTrigger: vi.fn(),
  setWeatherDesc: vi.fn(),
  setMenuOpen: vi.fn(),
  onOpenHistory: vi.fn(),
  onOpenQueue: vi.fn(),
  onOpenRooms: vi.fn(),
  onOpenSettings: vi.fn(),
  onOpenHelp: vi.fn(),
  onOpenWeatherSettings: vi.fn(),
  onOpenWeatherForecast: vi.fn(),
  onOpenBugReport: vi.fn(),
  onToggleAgentDrawer: vi.fn(),
};

function renderTopBar(props = {}) {
  render(
    <TopBar
      currentTime={new Date('2026-06-15T17:30:00')}
      formatTime={() => '5:30 PM'}
      formatDate={() => 'Monday, June 15'}
      weatherLocation={null}
      weatherCondition="clear"
      weatherTemp="--"
      weatherDesc="Weather unavailable"
      weatherRetryTrigger={0}
      menuOpen={false}
      userSettings={{ time_display_format: 'auto' }}
      activeAgentCount={0}
      agentDrawerExpanded={false}
      {...handlers}
      {...props}
    />,
  );
}

describe('TopBar spoke surface', () => {
  it('shows the desktop weather setup affordance on the hub SmartDisplay', () => {
    renderTopBar();

    expect(screen.getByText('Set your location')).toBeInTheDocument();
  });

  it('shows Settings-backed weather setup on a multiroom spoke', () => {
    renderTopBar({ isSpoke: true });

    expect(screen.getByText('Set your location')).toBeInTheDocument();
    expect(screen.getByTitle('Set your weather location in Settings')).toBeInTheDocument();
  });

  it('draws the unknown glyph when the condition is unknown', () => {
    renderTopBar({
      weatherLocation: 'Phoenix, AZ',
      weatherCondition: 'unknown',
      weatherTemp: '104°',
      weatherDesc: 'Condition unavailable',
    });

    expect(screen.getByTestId('weather-icon-unknown')).toBeInTheDocument();
    expect(screen.getByText('104°')).toBeInTheDocument();
  });

  it('draws a real glyph when the condition is known', () => {
    renderTopBar({
      weatherLocation: 'Chicago, IL',
      weatherCondition: 'rain',
      weatherTemp: '61°',
      weatherDesc: 'Light rain',
    });

    expect(screen.queryByTestId('weather-icon-unknown')).not.toBeInTheDocument();
  });
});
