/**
 * Icons sourced from Meteocons by Bas Milius, MIT license
 * (see LICENSES/meteocons.txt).
 */
import PropTypes from 'prop-types';

import { UNKNOWN_CONDITION, normalizeConditionKey } from '../../utils/weatherCondition';

import clearDayAnimated from '../../assets/meteocons/animated/clear-day.svg?url';
import clearNightAnimated from '../../assets/meteocons/animated/clear-night.svg?url';
import cloudyAnimated from '../../assets/meteocons/animated/cloudy.svg?url';
import drizzleAnimated from '../../assets/meteocons/animated/drizzle.svg?url';
import fogDayAnimated from '../../assets/meteocons/animated/fog-day.svg?url';
import fogNightAnimated from '../../assets/meteocons/animated/fog-night.svg?url';
import hazeDayAnimated from '../../assets/meteocons/animated/haze-day.svg?url';
import hazeNightAnimated from '../../assets/meteocons/animated/haze-night.svg?url';
import mistAnimated from '../../assets/meteocons/animated/mist.svg?url';
import partlyCloudyDayAnimated from '../../assets/meteocons/animated/partly-cloudy-day.svg?url';
import partlyCloudyNightAnimated from '../../assets/meteocons/animated/partly-cloudy-night.svg?url';
import rainAnimated from '../../assets/meteocons/animated/rain.svg?url';
import sleetAnimated from '../../assets/meteocons/animated/sleet.svg?url';
import snowAnimated from '../../assets/meteocons/animated/snow.svg?url';
import thunderstormsRainAnimated from '../../assets/meteocons/animated/thunderstorms-rain.svg?url';
import clearDayStatic from '../../assets/meteocons/static/clear-day.svg?url';
import clearNightStatic from '../../assets/meteocons/static/clear-night.svg?url';
import cloudyStatic from '../../assets/meteocons/static/cloudy.svg?url';
import drizzleStatic from '../../assets/meteocons/static/drizzle.svg?url';
import fogDayStatic from '../../assets/meteocons/static/fog-day.svg?url';
import fogNightStatic from '../../assets/meteocons/static/fog-night.svg?url';
import hazeDayStatic from '../../assets/meteocons/static/haze-day.svg?url';
import hazeNightStatic from '../../assets/meteocons/static/haze-night.svg?url';
import mistStatic from '../../assets/meteocons/static/mist.svg?url';
import partlyCloudyDayStatic from '../../assets/meteocons/static/partly-cloudy-day.svg?url';
import partlyCloudyNightStatic from '../../assets/meteocons/static/partly-cloudy-night.svg?url';
import rainStatic from '../../assets/meteocons/static/rain.svg?url';
import sleetStatic from '../../assets/meteocons/static/sleet.svg?url';
import snowStatic from '../../assets/meteocons/static/snow.svg?url';
import thunderstormsRainStatic from '../../assets/meteocons/static/thunderstorms-rain.svg?url';

const ICONS = {
  static: {
    clearDay: clearDayStatic,
    clearNight: clearNightStatic,
    cloudy: cloudyStatic,
    drizzle: drizzleStatic,
    fogDay: fogDayStatic,
    fogNight: fogNightStatic,
    hazeDay: hazeDayStatic,
    hazeNight: hazeNightStatic,
    mist: mistStatic,
    partlyCloudyDay: partlyCloudyDayStatic,
    partlyCloudyNight: partlyCloudyNightStatic,
    rain: rainStatic,
    sleet: sleetStatic,
    snow: snowStatic,
    thunderstormsRain: thunderstormsRainStatic,
  },
  animated: {
    clearDay: clearDayAnimated,
    clearNight: clearNightAnimated,
    cloudy: cloudyAnimated,
    drizzle: drizzleAnimated,
    fogDay: fogDayAnimated,
    fogNight: fogNightAnimated,
    hazeDay: hazeDayAnimated,
    hazeNight: hazeNightAnimated,
    mist: mistAnimated,
    partlyCloudyDay: partlyCloudyDayAnimated,
    partlyCloudyNight: partlyCloudyNightAnimated,
    rain: rainAnimated,
    sleet: sleetAnimated,
    snow: snowAnimated,
    thunderstormsRain: thunderstormsRainAnimated,
  },
};

const ICON_PROP_TYPES = {
  animated: PropTypes.bool,
};

const DAY_NIGHT_ICON_PROP_TYPES = {
  ...ICON_PROP_TYPES,
  isDay: PropTypes.bool,
};

const selectDayNightIcon = (isDay, dayIcon, nightIcon) => (isDay ? dayIcon : nightIcon);

const getIconUrl = (name, animated) => {
  const variant = animated ? ICONS.animated : ICONS.static;
  return variant[name] || null;
};

/**
 * Drawn for a condition we do not know. It deliberately looks like missing
 * data rather than weather: a Meteocon here (the old fallback was a cheerful
 * partly-cloudy glyph) reads as a forecast, so an empty payload looked sunny.
 */
export const UnknownConditionIcon = () => (
  <svg
    width="64"
    height="64"
    viewBox="0 0 64 64"
    role="img"
    aria-label="Weather condition unavailable"
    data-testid="weather-icon-unknown"
    style={{ display: 'block', opacity: 0.55 }}
  >
    <circle
      cx="32"
      cy="32"
      r="21"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeDasharray="5 6"
    />
    <path
      d="M26.5 26.5a5.5 5.5 0 1 1 6.6 5.4v4.1"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <circle cx="33.1" cy="42.5" r="1.9" fill="currentColor" />
  </svg>
);

const MeteoconIcon = ({ name, animated = false }) => {
  const src = getIconUrl(name, animated);
  if (!src) return <UnknownConditionIcon />;

  return (
    <img
      src={src}
      width="64"
      height="64"
      alt=""
      aria-hidden="true"
      draggable="false"
      style={{ display: 'block', objectFit: 'contain' }}
    />
  );
};

MeteoconIcon.propTypes = {
  name: PropTypes.string.isRequired,
  animated: PropTypes.bool,
};

export const SunIcon = ({ animated = false }) => (
  <MeteoconIcon name="clearDay" animated={animated} />
);

SunIcon.propTypes = ICON_PROP_TYPES;

export const MoonIcon = ({ animated = false }) => (
  <MeteoconIcon name="clearNight" animated={animated} />
);

MoonIcon.propTypes = ICON_PROP_TYPES;

export const PartlyCloudyIcon = ({ animated = false }) => (
  <MeteoconIcon name="partlyCloudyDay" animated={animated} />
);

PartlyCloudyIcon.propTypes = ICON_PROP_TYPES;

export const PartlyCloudyNightIcon = ({ animated = false }) => (
  <MeteoconIcon name="partlyCloudyNight" animated={animated} />
);

PartlyCloudyNightIcon.propTypes = ICON_PROP_TYPES;

export const CloudyIcon = ({ animated = false }) => (
  <MeteoconIcon name="cloudy" animated={animated} />
);

CloudyIcon.propTypes = ICON_PROP_TYPES;

export const RainIcon = ({ animated = false }) => (
  <MeteoconIcon name="rain" animated={animated} />
);

RainIcon.propTypes = ICON_PROP_TYPES;

export const StormIcon = ({ animated = false }) => (
  <MeteoconIcon name="thunderstormsRain" animated={animated} />
);

StormIcon.propTypes = ICON_PROP_TYPES;

export const SnowIcon = ({ animated = false }) => (
  <MeteoconIcon name="snow" animated={animated} />
);

SnowIcon.propTypes = ICON_PROP_TYPES;

export const SleetIcon = ({ animated = false }) => (
  <MeteoconIcon name="sleet" animated={animated} />
);

SleetIcon.propTypes = ICON_PROP_TYPES;

export const FogIcon = ({ animated = false, isDay = true }) => (
  <MeteoconIcon name={selectDayNightIcon(isDay, 'fogDay', 'fogNight')} animated={animated} />
);

FogIcon.propTypes = DAY_NIGHT_ICON_PROP_TYPES;

export const getConditionIconName = (condition, isDay) => {
  const key = normalizeConditionKey(condition);

  const DAY_MAP = {
    'clear': 'clearDay',
    'partly-cloudy': 'partlyCloudyDay',
  };

  const NIGHT_MAP = {
    'clear': 'clearNight',
    'partly-cloudy': 'partlyCloudyNight',
  };

  const EXPLICIT = {
    'clear-night': 'clearNight',
    'partly-cloudy-night': 'partlyCloudyNight',
  };

  const NEUTRAL = {
    'cloudy': 'cloudy',
    'overcast': 'cloudy',
    'rain': 'rain',
    'drizzle': 'drizzle',
    'storm': 'thunderstormsRain',
    'snow': 'snow',
    'sleet': 'sleet',
    'hail': 'sleet',
    'mist': 'mist',
  };

  const DAY_NIGHT = {
    'fog': selectDayNightIcon(isDay, 'fogDay', 'fogNight'),
    'haze': selectDayNightIcon(isDay, 'hazeDay', 'hazeNight'),
    'smoke': selectDayNightIcon(isDay, 'hazeDay', 'hazeNight'),
    'dust': selectDayNightIcon(isDay, 'hazeDay', 'hazeNight'),
  };

  const rawKey = String(condition || '').toLowerCase().replace(/_/g, '-');
  if (EXPLICIT[rawKey]) return EXPLICIT[rawKey];
  if (NEUTRAL[key]) return NEUTRAL[key];
  if (DAY_NIGHT[key]) return DAY_NIGHT[key];

  const dayNightMap = isDay ? DAY_MAP : NIGHT_MAP;
  if (dayNightMap[key]) return dayNightMap[key];

  // Includes `wind`, which says nothing about the sky, and anything this build
  // has no icon for. Both are honestly unknown rather than quietly cheerful.
  return UNKNOWN_CONDITION;
};

// Day/night aware. `animated` is intentionally opt-in so dense forecast lists
// stay on the static Meteocons variants while the popout hero can animate.
export const WeatherIcon = ({ condition, isDay = true, animated = false }) => (
  <MeteoconIcon name={getConditionIconName(condition, isDay)} animated={animated} />
);

WeatherIcon.propTypes = {
  condition: PropTypes.string,
  isDay: PropTypes.bool,
  animated: PropTypes.bool,
};
