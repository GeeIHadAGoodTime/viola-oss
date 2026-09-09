import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '../test/test-utils';
import WeatherForecast from './WeatherForecast';

const theme = {
  colors: {
    accent: '#C89B3C',
    bgCard: '#0d0d0d',
    bgElevated: '#1a1a1a',
    borderHover: 'rgba(255,255,255,0.12)',
    borderLight: 'rgba(255,255,255,0.06)',
    glassBase: 'rgba(255,255,255,0.07)',
    glassHover: 'rgba(255,255,255,0.10)',
    overlay: 'rgba(0,0,0,0.85)',
    shadowDeep: 'rgba(0,0,0,0.8)',
    textMuted: 'rgba(255,255,255,0.45)',
    textPrimary: 'rgba(255,255,255,0.85)',
  },
};

const anchorRect = {
  left: 24,
  top: 12,
  right: 174,
  bottom: 56,
  width: 150,
  height: 44,
};

function makeForecastData() {
  const hourly = Array.from({ length: 24 }, (_, index) => ({
    time: `2026-05-12T${String(index).padStart(2, '0')}:00`,
    date: '2026-05-12',
    temperature_f: 70 + index % 6,
    feels_like_f: 68 + index % 6,
    condition: index % 3 === 0 ? 'Light rain' : 'Partly cloudy',
    condition_code: index % 3 === 0 ? 'rain' : 'partly-cloudy',
    precip_chance: index % 4 === 0 ? 40 : 10,
    wind_speed_mph: 9 + index % 5,
    wind_direction_cardinal: 'SW',
  }));

  const daily = Array.from({ length: 10 }, (_, index) => ({
    date: `2026-05-${String(12 + index).padStart(2, '0')}`,
    day: index === 0 ? 'Tuesday' : `Day ${index + 1}`,
    condition: index % 2 === 0 ? 'Light rain' : 'Mostly sunny',
    condition_code: index % 2 === 0 ? 'rain' : 'clear',
    high_f: 76 + index,
    low_f: 55 + index,
    precip_chance: 20 + index,
    precipitation_in: 0.02 * index,
    uv_index: 6 + index / 10,
    wind_speed_mph: 12 + index,
    wind_direction_cardinal: 'SSW',
    sunrise: `2026-05-${String(12 + index).padStart(2, '0')}T05:33`,
    sunset: `2026-05-${String(12 + index).padStart(2, '0')}T20:00`,
  }));

  return {
    location: 'Chicago, IL',
    temperature: 72,
    condition: 'Light rain',
    current: {
      temperature_f: 72,
      feels_like_f: 70,
      condition: 'Light rain',
      condition_code: 'rain',
      humidity: 62,
      pressure_hpa: 1011,
      dew_point_f: 58,
      visibility_miles: 9,
      wind_speed_mph: 12,
      wind_direction_cardinal: 'SSW',
      uv_index: 5.4,
      air_quality: {
        us_aqi: 42,
        category: 'Good',
        primary_pollutant: 'PM2.5',
        provider: 'EPA AirNow',
      },
      sunrise: '2026-05-12T05:33',
      sunset: '2026-05-12T20:00',
    },
    hourly_forecast: hourly,
    hourly,
    daily_forecast: daily,
    daily,
    forecast: daily,
  };
}

function renderForecast(overrides = {}) {
  const onClose = vi.fn();
  render(
    <WeatherForecast
      forecastData={makeForecastData()}
      theme={theme}
      anchorRect={anchorRect}
      onClose={onClose}
      {...overrides}
    />
  );
  return { onClose };
}

describe('WeatherForecast', () => {
  it('renders current conditions from mock forecast data', () => {
    renderForecast();

    expect(screen.getByTestId('weather-hero')).toBeInTheDocument();
    expect(screen.getByText('Chicago, IL')).toBeInTheDocument();
    expect(screen.getAllByText('Light rain').length).toBeGreaterThan(0);
    expect(screen.getByText(/Feels 70/)).toBeInTheDocument();
    expect(screen.getAllByTestId('weather-detail-card')).toHaveLength(11);
    expect(screen.getByText('Humidity')).toBeInTheDocument();
    expect(screen.getByText('UV Index')).toBeInTheDocument();
    expect(screen.getByText('Air Quality')).toBeInTheDocument();
    expect(screen.getByText('42 AQI')).toBeInTheDocument();
    expect(screen.getByText(/Good/)).toBeInTheDocument();
    expect(screen.getByText(/Primary PM2.5/)).toBeInTheDocument();
    expect(screen.getByText('From the south-southwest')).toBeInTheDocument();
  });

  it('renders a 24-entry hourly strip and 10 daily rows', () => {
    renderForecast();

    expect(screen.getAllByTestId('weather-hourly-entry')).toHaveLength(24);
    expect(screen.getAllByTestId('weather-daily-row')).toHaveLength(10);
    expect(screen.getAllByTestId('weather-temp-range')).toHaveLength(10);
    expect(screen.getByText('Now')).toBeInTheDocument();
    expect(screen.queryByText('10%')).not.toBeInTheDocument();
  });

  it('uses top-level temperature for the modal current observation and current hourly card', () => {
    const forecastData = makeForecastData();
    forecastData.temperature = 43;
    forecastData.temperature_f = 62;
    forecastData.generated_at = '2026-05-12T12:00:00';
    forecastData.current = {
      ...forecastData.current,
      temperature_f: 62,
      time: '2026-05-12T12:00:00',
    };
    forecastData.hourly_forecast[0] = {
      ...forecastData.hourly_forecast[0],
      time: '2026-05-12T12:00:00',
      temperature_f: 62,
    };

    renderForecast({ forecastData });

    expect(within(screen.getByTestId('weather-hero')).getByText('43°')).toBeInTheDocument();
    const firstHour = screen.getAllByTestId('weather-hourly-entry')[0];
    expect(within(firstHour).getByText('Now')).toBeInTheDocument();
    expect(within(firstHour).getByText('43°')).toBeInTheDocument();
    expect(within(firstHour).queryByText('62°')).not.toBeInTheDocument();
  });

  it('does not label a stale first hourly row as Now', () => {
    const forecastData = makeForecastData();
    forecastData.temperature = 43;
    forecastData.temperature_f = 62;
    forecastData.updated = '2026-05-12T12:00:00';
    forecastData.current = {
      ...forecastData.current,
      temperature_f: 62,
      time: '2026-05-12T08:00:00',
    };
    forecastData.hourly_forecast[0] = {
      ...forecastData.hourly_forecast[0],
      time: '2026-05-12T08:00:00',
      temperature_f: 62,
    };

    renderForecast({ forecastData });

    expect(within(screen.getByTestId('weather-hero')).getByText('43°')).toBeInTheDocument();
    const firstHour = screen.getAllByTestId('weather-hourly-entry')[0];
    expect(within(firstHour).queryByText('Now')).not.toBeInTheDocument();
    expect(within(firstHour).getByText('8 AM')).toBeInTheDocument();
    expect(within(firstHour).getByText('62°')).toBeInTheDocument();
  });

  it('closes when the user clicks outside the panel', () => {
    const { onClose } = renderForecast();

    fireEvent.mouseDown(screen.getByTestId('weather-forecast-overlay'));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes when Escape is pressed', () => {
    const { onClose } = renderForecast();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('WeatherForecast — per-day hourly expand', () => {
  it('shows the hourly breakdown when the backend returns hourly_forecast (not hourly)', () => {
    const data = makeForecastData();
    // Backend returned the `hourly_forecast` shape only. Before the fix,
    // hourlyByDate read only `hourly`, so the day-row expand rendered nothing.
    delete data.hourly;
    render(
      <WeatherForecast
        forecastData={data}
        theme={theme}
        anchorRect={anchorRect}
        onClose={vi.fn()}
      />
    );

    const firstRow = screen.getAllByTestId('weather-daily-row')[0];
    // Not expanded yet.
    expect(within(firstRow).queryByTestId('weather-daily-hours')).not.toBeInTheDocument();

    fireEvent.click(within(firstRow).getByRole('button'));

    const hours = within(firstRow).getByTestId('weather-daily-hours');
    expect(hours).toBeInTheDocument();
    // The first day (2026-05-12) has 24 hours; the expand shows up to 8.
    expect(hours.children.length).toBe(8);
  });
});

describe('WeatherForecast — an unknown condition stays unknown', () => {
  it('draws the unknown glyph and says so instead of inventing partly cloudy', () => {
    const data = makeForecastData();
    // The shape /v1/weather returns when no provider reported a sky state:
    // no condition text anywhere, condition_code "unknown".
    delete data.condition;
    delete data.description;
    data.condition_code = 'unknown';
    delete data.current.condition;
    data.current.condition_code = 'unknown';
    data.hourly = data.hourly.map((hour) => {
      const next = { ...hour, condition_code: 'unknown' };
      delete next.condition;
      return next;
    });
    data.hourly_forecast = data.hourly;
    data.daily = data.daily.map((day) => {
      const next = { ...day, condition_code: 'unknown' };
      delete next.condition;
      return next;
    });
    data.daily_forecast = data.daily;

    render(
      <WeatherForecast
        forecastData={data}
        theme={theme}
        anchorRect={anchorRect}
        onClose={vi.fn()}
      />
    );

    expect(screen.getByText('Condition unavailable')).toBeInTheDocument();
    expect(screen.getAllByText('Unavailable').length).toBe(10);
    expect(screen.queryByAltText(/partly/i)).not.toBeInTheDocument();

    const hero = screen.getByTestId('weather-hero');
    expect(within(hero).getByTestId('weather-icon-unknown')).toBeInTheDocument();

    // Every hourly and daily row draws the unknown glyph too: 1 hero + 24
    // hours + 10 days.
    expect(screen.getAllByTestId('weather-icon-unknown')).toHaveLength(35);
  });

  it('keeps drawing real icons when the payload does report a condition', () => {
    renderForecast();

    const hero = screen.getByTestId('weather-hero');
    expect(within(hero).queryByTestId('weather-icon-unknown')).not.toBeInTheDocument();
    expect(screen.getAllByText('Light rain').length).toBeGreaterThan(0);
  });
});
