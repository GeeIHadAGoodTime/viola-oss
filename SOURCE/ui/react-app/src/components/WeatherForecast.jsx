import { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { WeatherDetailIcon } from './WeatherDetailIcons';
import { WeatherIcon } from './topbar/WeatherIcons';
import styles from './WeatherForecast.module.css';
import { describeCondition, normalizeConditionKey } from '../utils/weatherCondition';

const FORECAST_PANEL_MAX_WIDTH = 860;
const FORECAST_PANEL_GUTTER = 16;
const DEGREE = '\u00b0';
const MIDDLE_DOT = '\u00b7';
const CARDINAL_DEGREES = {
  N: 0,
  NNE: 22.5,
  NE: 45,
  ENE: 67.5,
  E: 90,
  ESE: 112.5,
  SE: 135,
  SSE: 157.5,
  S: 180,
  SSW: 202.5,
  SW: 225,
  WSW: 247.5,
  W: 270,
  WNW: 292.5,
  NW: 315,
  NNW: 337.5,
};

const CARDINAL_LABELS = {
  N: 'north',
  NNE: 'north-northeast',
  NE: 'northeast',
  ENE: 'east-northeast',
  E: 'east',
  ESE: 'east-southeast',
  SE: 'southeast',
  SSE: 'south-southeast',
  S: 'south',
  SSW: 'south-southwest',
  SW: 'southwest',
  WSW: 'west-southwest',
  W: 'west',
  WNW: 'west-northwest',
  NW: 'northwest',
  NNW: 'north-northwest',
};

const ATMOSPHERES = {
  dawn: {
    top: '#263a62',
    mid: '#1c3559',
    bottom: '#0b1325',
    glow: 'rgba(255, 178, 111, 0.22)',
  },
  day: {
    top: '#16385f',
    mid: '#10284d',
    bottom: '#071326',
    glow: 'rgba(126, 194, 255, 0.18)',
  },
  dusk: {
    top: '#302c56',
    mid: '#1f284b',
    bottom: '#080d1c',
    glow: 'rgba(255, 143, 103, 0.20)',
  },
  night: {
    top: '#111b33',
    mid: '#0b1428',
    bottom: '#050814',
    glow: 'rgba(107, 156, 255, 0.14)',
  },
};

function toNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toCelsius(fahrenheit) {
  const value = toNumber(fahrenheit);
  return value === null ? null : Math.round((value - 32) * 5 / 9);
}

function formatTemp(value, unitsPreference = 'imperial') {
  const number = toNumber(value);
  if (number === null) return '--';
  if (unitsPreference === 'metric') return `${toCelsius(number)}${DEGREE}C`;
  return `${Math.round(number)}${DEGREE}`;
}

function formatWind(value, unitsPreference = 'imperial') {
  const number = toNumber(value);
  if (number === null) return '--';
  if (unitsPreference === 'metric') return `${Math.round(number * 1.60934)} km/h`;
  return `${Math.round(number)} mph`;
}

function formatPrecip(value, unitsPreference = 'imperial') {
  const number = toNumber(value);
  if (number === null) return '--';
  if (unitsPreference === 'metric') return `${Math.round(number * 25.4)} mm`;
  return `${number.toFixed(number < 0.1 ? 2 : 1)} in`;
}

function formatVisibility(value, unitsPreference = 'imperial') {
  const number = toNumber(value);
  if (number === null) return '--';
  if (unitsPreference === 'metric') return `${Math.round(number * 1.60934)} km`;
  return `${Math.round(number)} mi`;
}

function formatPressure(value) {
  const number = toNumber(value);
  return number === null ? '--' : `${Math.round(number)} hPa`;
}

function formatPercent(value) {
  const number = toNumber(value);
  return number === null ? '--' : `${Math.round(number)}%`;
}

function getPrecipChance(...values) {
  for (const value of values) {
    const number = toNumber(value);
    if (number !== null) return Math.max(0, Math.min(100, Math.round(number)));
  }
  return null;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function formatUvValue(value) {
  const number = toNumber(value);
  if (number === null) return '--';
  return String(Math.round(number));
}

function describeUv(value) {
  const number = toNumber(value);
  if (number === null) return 'No reading';
  if (number < 3) return 'Low';
  if (number < 6) return 'Moderate';
  if (number < 8) return 'High';
  if (number < 11) return 'Very high';
  return 'Extreme';
}

function describeHumidity(value) {
  const number = toNumber(value);
  if (number === null) return 'No reading';
  if (number < 35) return 'Dry air';
  if (number <= 60) return 'Comfortable';
  if (number <= 75) return 'Humid';
  return 'Very humid';
}

function describeVisibility(value) {
  const number = toNumber(value);
  if (number === null) return 'No reading';
  if (number >= 8) return 'Clear view';
  if (number >= 4) return 'Moderate';
  return 'Reduced';
}

function describeDewPoint(value) {
  const number = toNumber(value);
  if (number === null) return 'No reading';
  if (number < 55) return 'Dry';
  if (number < 65) return 'Comfortable';
  if (number < 70) return 'Muggy';
  return 'Very muggy';
}

function describePressure(value) {
  const number = toNumber(value);
  if (number === null) return 'No reading';
  if (number < 1000) return 'Low pressure';
  if (number > 1022) return 'High pressure';
  return 'Steady';
}

function describeAirQuality(value, category) {
  if (category) return category;
  const number = toNumber(value);
  if (number === null) return 'No reading';
  if (number <= 50) return 'Good';
  if (number <= 100) return 'Moderate';
  if (number <= 150) return 'Unhealthy for sensitive groups';
  if (number <= 200) return 'Unhealthy';
  if (number <= 300) return 'Very unhealthy';
  return 'Hazardous';
}

function getAirQualityReading(current, forecastData) {
  const candidates = [
    current.air_quality,
    forecastData?.air_quality,
    current.us_aqi,
    current.aqi,
    forecastData?.us_aqi,
    forecastData?.aqi,
  ];

  for (const candidate of candidates) {
    if (candidate && typeof candidate === 'object') {
      const aqi = toNumber(candidate.us_aqi ?? candidate.aqi ?? candidate.index);
      if (aqi !== null) {
        return {
          aqi,
          category: candidate.category,
          pm25: toNumber(candidate.pm2_5 ?? candidate.pm25),
          primaryPollutant: candidate.primary_pollutant ?? candidate.pollutant ?? null,
          provider: candidate.provider,
        };
      }
    } else {
      const aqi = toNumber(candidate);
      if (aqi !== null) return { aqi, category: null, pm25: null, primaryPollutant: null, provider: null };
    }
  }

  return { aqi: null, category: null, pm25: null, primaryPollutant: null, provider: null };
}

function formatAirQuality(value) {
  const number = toNumber(value);
  return number === null ? '--' : `${Math.round(number)} AQI`;
}

function formatAirQualityNote(reading) {
  const label = describeAirQuality(reading.aqi, reading.category);
  if (reading.pm25 !== null) return `${label} ${MIDDLE_DOT} PM2.5 ${reading.pm25}`;
  if (reading.primaryPollutant) return `${label} ${MIDDLE_DOT} Primary ${reading.primaryPollutant}`;
  return label;
}

function formatDirectionName(value) {
  const key = String(value || '').toUpperCase().trim();
  return CARDINAL_LABELS[key] || String(value || '').toLowerCase();
}

function getWindDegrees(current, forecastData) {
  const degrees = toNumber(current.wind_direction_degrees ?? current.wind_degrees ?? forecastData?.wind_direction_degrees);
  if (degrees !== null) return clamp(degrees, 0, 360);
  const cardinal = String(current.wind_direction_cardinal || forecastData?.wind_direction_cardinal || '').toUpperCase().trim();
  return CARDINAL_DEGREES[cardinal] ?? 0;
}

function getDaylightNote(primaryTime, fallbackLabel, fallbackTime) {
  if (fallbackTime && fallbackTime !== '--') return `${fallbackLabel} ${fallbackTime}`;
  if (primaryTime && primaryTime !== '--') return 'Local time';
  return 'No reading';
}

function parseDateValue(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function getAtmosphere(current, firstDay, forecastData) {
  if (current.is_day === false || current.is_night === true) return ATMOSPHERES.night;
  if (current.is_day === true) return ATMOSPHERES.day;

  const now = parseDateValue(
    current.time
    || current.observation_time
    || forecastData?.generated_at
    || forecastData?.updated_at,
  ) || new Date();
  const sunrise = parseDateValue(current.sunrise ?? firstDay.sunrise);
  const sunset = parseDateValue(current.sunset ?? firstDay.sunset);
  if (!sunrise || !sunset) return ATMOSPHERES.day;

  const minutesFromSunrise = Math.abs(now.getTime() - sunrise.getTime()) / 60000;
  const minutesFromSunset = Math.abs(now.getTime() - sunset.getTime()) / 60000;
  if (now < sunrise || now > sunset) return ATMOSPHERES.night;
  if (minutesFromSunrise < 100) return ATMOSPHERES.dawn;
  if (minutesFromSunset < 120) return ATMOSPHERES.dusk;
  return ATMOSPHERES.day;
}

function formatTime(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(11, 16) || String(value);
  return date.toLocaleTimeString([], { hour: 'numeric' });
}

function getCanonicalCurrentObservation(forecastData) {
  const nestedCurrent = forecastData?.current || {};
  const current = { ...(forecastData || {}), ...nestedCurrent };
  const topLevelTemperature = toNumber(forecastData?.temperature ?? forecastData?.temperature_f);

  if (topLevelTemperature !== null) {
    current.temperature_f = topLevelTemperature;
    current.temperature = topLevelTemperature;
  }

  return current;
}

function getObservationDate(current, forecastData) {
  return parseDateValue(
    forecastData?.observation_time
    || forecastData?.updated
    || forecastData?.generated_at
    || forecastData?.updated_at
    || current.observation_time
    || current.updated
    || current.updated_at
    || current.time,
  );
}

function isCurrentHour(hour, index, current, forecastData) {
  if (index !== 0) return false;

  const hourTime = parseDateValue(hour?.time || hour?.hour);
  const observationTime = getObservationDate(current, forecastData);
  if (!hourTime || !observationTime) return true;

  const minutesFromObservation = (hourTime.getTime() - observationTime.getTime()) / 60000;
  return minutesFromObservation >= -30 && minutesFromObservation <= 90;
}

function formatClock(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ').slice(-5);
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

// Icon + label resolution is shared with the topbar (utils/weatherCondition.js)
// so both surfaces answer "what is the sky doing?" from the same rules, and
// both can say they do not know.

function getDailyLabel(day, index) {
  if (index === 0) return 'Today';
  if (index === 1) return 'Tomorrow';
  if (day?.day) return day.day;
  if (!day?.date) return `Day ${index + 1}`;
  const parsed = new Date(`${day.date}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return day.date;
  return parsed.toLocaleDateString([], { weekday: 'long' });
}

function getPanelPosition(anchorRect) {
  // The forecast panel is centered on screen. The entry animation still
  // emanates from the weather widget anchor (via transform-origin) so the
  // pop-out feels rooted in the widget that was clicked, but the resting
  // position is dead-center — matches iOS Weather and native macOS sheets.
  if (typeof window === 'undefined') {
    return { left: FORECAST_PANEL_GUTTER, top: 82, originX: 48, originY: 0 };
  }

  const viewportWidth = window.innerWidth || 1200;
  const viewportHeight = window.innerHeight || 800;
  const panelWidth = Math.min(FORECAST_PANEL_MAX_WIDTH, viewportWidth - FORECAST_PANEL_GUTTER * 2);
  // Approximate panel height for vertical centering. The real panel scrolls
  // internally when content overflows max-height, so we don't need exact.
  const approxPanelHeight = Math.min(viewportHeight - FORECAST_PANEL_GUTTER * 2, 720);
  const left = Math.round((viewportWidth - panelWidth) / 2);
  const top = Math.max(
    FORECAST_PANEL_GUTTER,
    Math.round((viewportHeight - approxPanelHeight) / 2),
  );

  if (anchorRect) {
    // Animation origin: point from the widget that was clicked, expressed in
    // the panel's own coordinate space.
    const originX = Math.max(0, Math.min(panelWidth, anchorRect.left + anchorRect.width / 2 - left));
    const originY = Math.max(0, anchorRect.top + anchorRect.height / 2 - top);
    return { left, top, originX, originY };
  }
  return { left, top, originX: panelWidth / 2, originY: 0 };
}

function buildDetailCards({ current, firstDay, forecastData, unitsPreference }) {
  const humidity = toNumber(current.humidity ?? forecastData?.humidity);
  const windSpeed = current.wind_speed_mph ?? forecastData?.wind_speed_mph;
  const windDirection = current.wind_direction_cardinal || forecastData?.wind_direction_cardinal || '';
  const windText = `${formatWind(windSpeed, unitsPreference)} ${windDirection}`.trim();
  const precipChance = getPrecipChance(firstDay.precip_chance, current.precip_chance);
  const precipAmount = firstDay.precipitation_in ?? current.precipitation_in;
  const uvIndex = current.uv_index ?? firstDay.uv_index;
  const airQuality = getAirQualityReading(current, forecastData);
  const pressure = current.pressure_hpa ?? forecastData?.pressure_hpa;
  const visibility = current.visibility_miles ?? forecastData?.visibility_miles;
  const dewPoint = current.dew_point_f ?? forecastData?.dew_point_f;
  const sunrise = formatClock(current.sunrise ?? firstDay.sunrise);
  const sunset = formatClock(current.sunset ?? firstDay.sunset);

  return [
    {
      id: 'feels-like',
      icon: 'feelsLike',
      label: 'Feels Like',
      value: formatTemp(current.feels_like_f ?? current.feels_like ?? forecastData?.feels_like_f, unitsPreference),
      note: `Actual ${formatTemp(current.temperature_f ?? forecastData?.temperature, unitsPreference)}`,
      visual: 'thermo',
      marker: clamp(((toNumber(current.feels_like_f ?? current.feels_like) ?? 50) + 10) / 120 * 100, 6, 94),
    },
    {
      id: 'humidity',
      icon: 'humidity',
      label: 'Humidity',
      value: formatPercent(humidity),
      note: describeHumidity(humidity),
      visual: 'column',
      marker: humidity === null ? 0 : clamp(humidity, 0, 100),
    },
    {
      id: 'wind',
      icon: 'wind',
      label: 'Wind',
      value: windText,
      note: windDirection ? `From the ${formatDirectionName(windDirection)}` : 'No direction',
      visual: 'compass',
      marker: getWindDegrees(current, forecastData),
    },
    {
      id: 'precip',
      icon: 'precip',
      label: 'Precip',
      value: precipChance === null ? '--' : `${precipChance}%`,
      note: `${formatPrecip(precipAmount, unitsPreference)} expected`,
      visual: 'scale',
      marker: precipChance === null ? 0 : precipChance,
      tone: 'rain',
    },
    {
      id: 'uv-index',
      icon: 'uv',
      label: 'UV Index',
      value: formatUvValue(uvIndex),
      note: describeUv(uvIndex),
      visual: 'uv',
      marker: clamp(((toNumber(uvIndex) ?? 0) / 11) * 100, 0, 100),
    },
    {
      id: 'air-quality',
      icon: 'airQuality',
      label: 'Air Quality',
      value: formatAirQuality(airQuality.aqi),
      note: formatAirQualityNote(airQuality),
      visual: 'scale',
      marker: airQuality.aqi === null ? 0 : clamp((airQuality.aqi / 200) * 100, 0, 100),
      tone: 'airQuality',
    },
    {
      id: 'pressure',
      icon: 'pressure',
      label: 'Pressure',
      value: formatPressure(pressure),
      note: describePressure(pressure),
      visual: 'pulse',
    },
    {
      id: 'visibility',
      icon: 'visibility',
      label: 'Visibility',
      value: formatVisibility(visibility, unitsPreference),
      note: describeVisibility(visibility),
      visual: 'scale',
      marker: clamp(((toNumber(visibility) ?? 0) / 10) * 100, 0, 100),
      tone: 'visibility',
    },
    {
      id: 'dew-point',
      icon: 'dewPoint',
      label: 'Dew Point',
      value: formatTemp(dewPoint, unitsPreference),
      note: describeDewPoint(dewPoint),
      visual: 'thermo',
      marker: clamp(((toNumber(dewPoint) ?? 45) + 10) / 120 * 100, 6, 94),
    },
    {
      id: 'sunrise',
      icon: 'sunrise',
      label: 'Sunrise',
      value: sunrise,
      note: getDaylightNote(sunrise, 'Until', sunset),
      visual: 'arc',
      marker: 24,
    },
    {
      id: 'sunset',
      icon: 'sunset',
      label: 'Sunset',
      value: sunset,
      note: getDaylightNote(sunset, 'Since', sunrise),
      visual: 'arc',
      marker: 76,
    },
  ];
}

function DetailVisualization({ card }) {
  if (card.visual === 'compass') {
    return (
      <div className={styles.compassViz} aria-hidden="true">
        <span className={styles.compassNorth}>N</span>
        <span className={styles.compassNeedle} style={{ transform: `rotate(${card.marker}deg)` }} />
      </div>
    );
  }

  if (card.visual === 'column') {
    return (
      <div className={styles.columnViz} aria-hidden="true">
        <span style={{ height: `${card.marker}%` }} />
      </div>
    );
  }

  if (card.visual === 'uv') {
    return (
      <div className={styles.uvViz} aria-hidden="true">
        <span className={styles.vizMarker} style={{ left: `${card.marker}%` }} />
      </div>
    );
  }

  if (card.visual === 'arc') {
    return (
      <svg className={styles.arcViz} viewBox="0 0 104 40" aria-hidden="true">
        <path d="M12 31a40 40 0 0 1 80 0" />
        <path d="M8 31h88" />
        <circle cx={12 + card.marker * 0.8} cy={31 - Math.sin((card.marker / 100) * Math.PI) * 28} r="4" />
      </svg>
    );
  }

  if (card.visual === 'thermo') {
    return (
      <div className={styles.thermoViz} aria-hidden="true">
        <span style={{ width: `${card.marker}%` }} />
      </div>
    );
  }

  if (card.visual === 'pulse') {
    return (
      <div className={styles.pulseViz} aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    );
  }

  return (
    <div className={`${styles.scaleViz} ${card.tone ? styles[`${card.tone}Viz`] : ''}`} aria-hidden="true">
      <span style={{ width: `${card.marker}%` }} />
    </div>
  );
}

DetailVisualization.propTypes = {
  card: PropTypes.shape({
    marker: PropTypes.number,
    tone: PropTypes.string,
    visual: PropTypes.string,
  }).isRequired,
};

export default function WeatherForecast({
  forecastData,
  unitsPreference = 'imperial',
  theme,
  onClose,
  anchorRect,
  loading = false,
  error = '',
}) {
  const [expandedDate, setExpandedDate] = useState(null);
  const colors = theme?.colors || {};
  const panelPosition = getPanelPosition(anchorRect);

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const hourly = useMemo(() => (
    forecastData?.hourly_forecast
    || forecastData?.hourly
    || []
  ).slice(0, 24), [forecastData]);

  const daily = useMemo(() => (
    forecastData?.daily_forecast
    || forecastData?.daily
    || forecastData?.forecast
    || []
  ).slice(0, 10), [forecastData]);

  const dailyTempScale = useMemo(() => {
    const lows = daily.map((day) => toNumber(day.low_f ?? day.low)).filter((value) => value !== null);
    const highs = daily.map((day) => toNumber(day.high_f ?? day.high)).filter((value) => value !== null);
    const globalLow = lows.length ? Math.min(...lows) : 0;
    const globalHigh = highs.length ? Math.max(...highs) : globalLow + 1;
    return { globalLow, globalHigh, span: Math.max(1, globalHigh - globalLow) };
  }, [daily]);

  const hourlyByDate = useMemo(() => {
    const grouped = {};
    // Read the same `hourly_forecast || hourly` fallback the top hourly strip
    // uses (see the `hourly` memo above). Reading only `hourly` here left the
    // per-day expand empty whenever the backend returned `hourly_forecast`, so
    // clicking a day row toggled it open but rendered no hourly breakdown.
    for (const hour of forecastData?.hourly_forecast || forecastData?.hourly || []) {
      const date = hour.date || String(hour.time || '').slice(0, 10);
      if (!date) continue;
      if (!grouped[date]) grouped[date] = [];
      grouped[date].push(hour);
    }
    return grouped;
  }, [forecastData]);

  const current = getCanonicalCurrentObservation(forecastData);

  // Client-side day/night classifier. The backend's `is_daytime` field has
  // been observed wrong (1 PM local marked as night, 1 AM local marked as
  // day) — and the sunrise/sunset ISO strings are correct in *local-time
  // hour* but wrap UTC dates in a way that breaks naive date-based lookup.
  // Use local hour-of-day directly: convert the popout's sunrise/sunset to
  // local hours once, then for each hour entry compare its local hour.
  const localToHour = (iso, fallback) => {
    if (!iso) return fallback;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return fallback;
    return d.getHours() + d.getMinutes() / 60;
  };
  const firstDayForBounds = (forecastData?.daily_forecast || forecastData?.daily || forecastData?.forecast || [])[0] || {};
  const sunriseLocalHour = localToHour(current?.sunrise ?? firstDayForBounds.sunrise, 6);
  const sunsetLocalHour = localToHour(current?.sunset ?? firstDayForBounds.sunset, 20);

  const isHourDaytime = (hour) => {
    if (!hour) return true;
    const t = new Date(hour.time || hour.hour);
    if (Number.isNaN(t.getTime())) return hour.is_daytime !== false;
    const lh = t.getHours() + t.getMinutes() / 60;
    return lh >= sunriseLocalHour && lh < sunsetLocalHour;
  };

  const currentIsDaytime = (() => {
    const nowLh = new Date().getHours() + new Date().getMinutes() / 60;
    return nowLh >= sunriseLocalHour && nowLh < sunsetLocalHour;
  })();
  const firstDay = daily[0] || {};
  const conditionCode = current.condition_code || forecastData?.condition_code;
  const condition = describeCondition(current.condition || forecastData?.condition, conditionCode);
  const iconCondition = normalizeConditionKey(conditionCode, current.condition, forecastData?.condition);
  const atmosphere = getAtmosphere(current, firstDay, forecastData);
  const todayHighReference = toNumber(firstDay.high_f ?? firstDay.high);

  const detailCards = buildDetailCards({ current, firstDay, forecastData, unitsPreference });

  return (
    <div
      className={styles.overlay}
      data-testid="weather-forecast-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      style={{
        '--weather-overlay-bg': colors.overlay || 'rgba(0,0,0,0.82)',
        '--weather-bg-top': colors.bgElevated || '#1a1a1a',
        '--weather-bg-bottom': colors.bgCard || '#0d0d0d',
        '--weather-border': colors.borderHover || 'rgba(255,255,255,0.12)',
        '--weather-border-light': colors.borderLight || 'rgba(255,255,255,0.06)',
        '--weather-text': colors.textPrimary || 'rgba(255,255,255,0.85)',
        '--weather-muted': colors.textMuted || 'rgba(255,255,255,0.55)',
        '--weather-accent': colors.accent || '#C89B3C',
        '--weather-glass': colors.glassBase || 'rgba(255,255,255,0.07)',
        '--weather-glass-hover': colors.glassHover || 'rgba(255,255,255,0.10)',
        '--weather-shadow': colors.shadowDeep || 'rgba(0,0,0,0.8)',
        '--weather-sky-top': atmosphere.top,
        '--weather-sky-mid': atmosphere.mid,
        '--weather-sky-bottom': atmosphere.bottom,
        '--weather-sky-glow': atmosphere.glow,
      }}
    >
      <section
        className={styles.panel}
        data-testid="weather-forecast-panel"
        onMouseDown={(event) => event.stopPropagation()}
        style={{
          left: `${panelPosition.left}px`,
          top: `${panelPosition.top}px`,
          maxHeight: `calc(100dvh - ${panelPosition.top + FORECAST_PANEL_GUTTER}px)`,
          transformOrigin: `${panelPosition.originX}px ${panelPosition.originY}px`,
        }}
      >
        <header className={`${styles.hero} ${styles.sectionReveal}`} data-testid="weather-hero" style={{ '--section-delay': '0ms' }}>
          <div className={styles.heroTop}>
            <div className={styles.location}>{forecastData?.location || 'Weather'}</div>
            <button type="button" className={styles.closeButton} onClick={onClose} aria-label="Close weather forecast">
              &times;
            </button>
          </div>
          <div className={styles.heroWeather}>
            <div className={styles.heroIcon}>
              <WeatherIcon condition={iconCondition} isDay={currentIsDaytime} animated />
            </div>
            <div className={styles.currentTemp}>{formatTemp(current.temperature_f ?? forecastData?.temperature, unitsPreference)}</div>
          </div>
          <div className={styles.condition}>{condition}</div>
          <div className={styles.heroMeta}>
            <span>Feels {formatTemp(current.feels_like_f ?? current.feels_like, unitsPreference)}</span>
            <span>H {formatTemp(firstDay.high_f ?? firstDay.high, unitsPreference)}</span>
            <span>L {formatTemp(firstDay.low_f ?? firstDay.low, unitsPreference)}</span>
          </div>
        </header>

        {loading && (
          <div className={styles.skeleton} data-testid="weather-forecast-loading">
            <span />
            <span />
            <span />
          </div>
        )}

        {error && !loading && <div className={styles.error}>{error}</div>}

        {!loading && !error && (
          <>
            <div className={`${styles.hourlyStrip} ${styles.sectionReveal}`} style={{ '--section-delay': '45ms' }} aria-label="Hourly forecast">
              {hourly.map((hour, index) => {
                const precipChance = getPrecipChance(hour.precip_chance, hour.precipitation_probability);
                const showPrecip = precipChance !== null && precipChance > 20;
                const isNow = isCurrentHour(hour, index, current, forecastData);
                return (
                  <div
                    className={`${styles.hourCard} ${isNow ? styles.hourCardNow : ''}`}
                    data-testid="weather-hourly-entry"
                    key={`${hour.time || hour.hour}-${index}`}
                    style={{ '--hour-precip': `${precipChance || 0}%` }}
                  >
                    <span className={styles.hourTime}>{isNow ? 'Now' : formatTime(hour.time || hour.hour)}</span>
                    <div className={styles.hourIcon}>
                      <WeatherIcon
                        condition={normalizeConditionKey(hour.condition_code, hour.condition)}
                        isDay={isHourDaytime(hour)}
                      />
                    </div>
                    <span className={styles.hourTemp}>
                      {formatTemp(isNow ? current.temperature_f : (hour.temperature_f ?? hour.temperature), unitsPreference)}
                    </span>
                    <span className={`${styles.hourPrecip} ${showPrecip ? styles.hourPrecipVisible : ''}`}>
                      {showPrecip ? `${precipChance}%` : ''}
                    </span>
                  </div>
                );
              })}
            </div>

            <div className={`${styles.detailGrid} ${styles.sectionReveal}`} style={{ '--section-delay': '90ms' }} aria-label="Weather details">
              {detailCards.map((card) => (
                <article className={styles.detailCard} data-testid="weather-detail-card" key={card.id}>
                  <div className={styles.detailHeader}>
                    <WeatherDetailIcon name={card.icon} />
                    <span>{card.label}</span>
                  </div>
                  <strong className={styles.detailValue}>{card.value}</strong>
                  <span className={styles.detailNote}>{card.note}</span>
                  <DetailVisualization card={card} />
                </article>
              ))}
            </div>

            <div className={`${styles.dailyList} ${styles.sectionReveal}`} style={{ '--section-delay': '135ms' }} aria-label="10-day forecast">
              {daily.map((day, index) => {
                const dateKey = day.date || `day-${index}`;
                const dayHours = hourlyByDate[dateKey] || [];
                const isExpanded = expandedDate === dateKey;
                const high = toNumber(day.high_f ?? day.high) ?? 0;
                const low = toNumber(day.low_f ?? day.low) ?? high;
                const rangeLeft = Math.max(0, Math.min(100, ((low - dailyTempScale.globalLow) / dailyTempScale.span) * 100));
                const rangeWidth = Math.max(4, Math.min(100 - rangeLeft, ((high - low) / dailyTempScale.span) * 100));
                const referenceLeft = todayHighReference === null ? null : clamp(((todayHighReference - dailyTempScale.globalLow) / dailyTempScale.span) * 100, 0, 100);
                const precipChance = getPrecipChance(day.precip_chance, day.precipitation_probability);
                const showPrecip = precipChance !== null && precipChance > 5;
                return (
                  <div className={styles.dailyItem} data-testid="weather-daily-row" key={dateKey}>
                    <button
                      type="button"
                      className={styles.dailyRow}
                      onClick={() => setExpandedDate(isExpanded ? null : dateKey)}
                      aria-expanded={isExpanded}
                    >
                      <span className={styles.dayName}>{getDailyLabel(day, index)}</span>
                      <WeatherIcon condition={normalizeConditionKey(day.condition_code, day.condition)} isDay />
                      <span className={styles.dayCondition}>
                        {describeCondition(day.condition, day.condition_code, { short: true })}
                      </span>
                      <span className={styles.dayPrecip}>{showPrecip ? `${precipChance}%` : ''}</span>
                      <span className={styles.rangeTrack} data-testid="weather-temp-range">
                        <span className={styles.rangeFill} style={{ left: `${rangeLeft}%`, width: `${rangeWidth}%` }} />
                        {referenceLeft !== null && <span className={styles.rangeReference} style={{ left: `${referenceLeft}%` }} />}
                      </span>
                      <span className={styles.dayTemps}>
                        <span className={styles.dayHigh}>{formatTemp(day.high_f ?? day.high, unitsPreference)}</span>
                        <span className={styles.dayLow}>{formatTemp(day.low_f ?? day.low, unitsPreference)}</span>
                      </span>
                    </button>
                    {isExpanded && dayHours.length > 0 && (
                      <div className={styles.dailyHours} data-testid="weather-daily-hours">
                        {dayHours.slice(0, 8).map((hour) => (
                          <span key={hour.time}>
                            {formatTime(hour.time)} {MIDDLE_DOT} {formatTemp(hour.temperature_f ?? hour.temperature, unitsPreference)} {MIDDLE_DOT} {formatPercent(hour.precip_chance)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <div className={styles.footer}>Data: NOAA GFS {MIDDLE_DOT} NWS {MIDDLE_DOT} EPA AirNow AQI</div>
          </>
        )}
      </section>
    </div>
  );
}

WeatherForecast.propTypes = {
  forecastData: PropTypes.object,
  unitsPreference: PropTypes.string,
  theme: PropTypes.object,
  onClose: PropTypes.func.isRequired,
  anchorRect: PropTypes.shape({
    left: PropTypes.number,
    top: PropTypes.number,
    right: PropTypes.number,
    bottom: PropTypes.number,
    width: PropTypes.number,
    height: PropTypes.number,
  }),
  loading: PropTypes.bool,
  error: PropTypes.string,
};
