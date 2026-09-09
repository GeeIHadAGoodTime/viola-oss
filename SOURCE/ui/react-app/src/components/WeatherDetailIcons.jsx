import PropTypes from 'prop-types';

const ICON_PATHS = {
  feelsLike: (
    <>
      <path d="M9.5 14.1V6.4a2.5 2.5 0 0 1 5 0v7.7a4.3 4.3 0 1 1-5 0Z" />
      <path d="M12 8v7.8" />
      <path d="M17.2 7.2c1.5.5 2.6 1.9 2.6 3.6 0 1.3-.7 2.5-1.7 3.2" />
    </>
  ),
  humidity: (
    <>
      <path d="M12 3.8s-5.7 5.8-5.7 9.8a5.7 5.7 0 0 0 11.4 0C17.7 9.6 12 3.8 12 3.8Z" />
      <path d="M9.2 14.1c.5 1.4 1.5 2.2 3.1 2.4" />
    </>
  ),
  wind: (
    <>
      <path d="M4.2 9.2h9.4a2.2 2.2 0 1 0-2.1-3" />
      <path d="M3.8 13h13.4a2.4 2.4 0 1 1-2.3 3.1" />
      <path d="M6.2 16.8h5.7" />
      <path d="M17.9 7.8h1.9" />
    </>
  ),
  precip: (
    <>
      <path d="M8 4.5s-3.5 3.7-3.5 6.3a3.5 3.5 0 0 0 7 0C11.5 8.2 8 4.5 8 4.5Z" />
      <path d="M16.5 6.5s-4 4.3-4 7.3a4 4 0 0 0 8 0c0-3-4-7.3-4-7.3Z" />
      <path d="M6 18h12" />
    </>
  ),
  uv: (
    <>
      <circle cx="12" cy="12" r="3.2" fill="currentColor" opacity="0.16" stroke="none" />
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.5v3" />
      <path d="M12 18.5v3" />
      <path d="m4.9 4.9 2.1 2.1" />
      <path d="m17 17 2.1 2.1" />
      <path d="M2.5 12h3" />
      <path d="M18.5 12h3" />
      <path d="m4.9 19.1 2.1-2.1" />
      <path d="m17 7 2.1-2.1" />
    </>
  ),
  airQuality: (
    <>
      <path d="M5 10.2c1.6-2 3.6-3 6-3 4.6 0 6.2 3.8 8 3.8.5 0 1-.1 1.5-.4" />
      <path d="M4 14.4c1.4-1.1 3-1.6 4.8-1.6 3.9 0 5.2 2.7 8.3 2.7 1.1 0 2.1-.3 2.9-.9" />
      <circle cx="8" cy="17.8" r="1" fill="currentColor" opacity="0.36" stroke="none" />
      <circle cx="12.5" cy="18.8" r="0.8" fill="currentColor" opacity="0.28" stroke="none" />
      <circle cx="16.5" cy="6.2" r="0.9" fill="currentColor" opacity="0.32" stroke="none" />
    </>
  ),
  pressure: (
    <>
      <path d="M5.5 15.8a6.5 6.5 0 1 1 13 0" />
      <path d="M7.5 15.8h9" />
      <path d="M12 12.5 15.8 8.7" />
      <path d="M8 19.2h8" />
      <path d="M8.2 12.2h.1" />
      <path d="M15.8 12.2h.1" />
    </>
  ),
  visibility: (
    <>
      <path d="M3 12s3.2-5 9-5 9 5 9 5-3.2 5-9 5-9-5-9-5Z" />
      <circle cx="12" cy="12" r="2.5" />
    </>
  ),
  dewPoint: (
    <>
      <path d="M10 14.5V5.8a2.8 2.8 0 1 1 5.6 0v8.7a4.8 4.8 0 1 1-5.6 0Z" />
      <path d="M12.8 9.5v6" />
      <path d="M18.5 13.5s-2 2.1-2 3.5a2 2 0 0 0 4 0c0-1.4-2-3.5-2-3.5Z" />
    </>
  ),
  sunrise: (
    <>
      <path d="M4 18h16" />
      <path d="M7 18a5 5 0 0 1 10 0" />
      <path d="M12 3v8" />
      <path d="m8.5 6.5 3.5-3.5 3.5 3.5" />
    </>
  ),
  sunset: (
    <>
      <path d="M4 18h16" />
      <path d="M7 18a5 5 0 0 1 10 0" />
      <path d="M12 3v8" />
      <path d="m8.5 7.5 3.5 3.5 3.5-3.5" />
    </>
  ),
};

export function WeatherDetailIcon({ name }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.08" stroke="none" />
      {ICON_PATHS[name] || ICON_PATHS.feelsLike}
    </svg>
  );
}

WeatherDetailIcon.propTypes = {
  name: PropTypes.string.isRequired,
};
