/* eslint react/jsx-uses-vars: "error" */
/**
 * TopBar — Clock, weather, date, menu, and calendar section.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import ErrorBoundary from '../ErrorBoundary';
import CalendarView from '../CalendarView';
import { WeatherIcon } from './WeatherIcons';
import MenuButton from './MenuButton';
import DropdownMenu from './DropdownMenu';
import styles from './TopBar.module.css';

const TopBar = ({
  currentTime,
  formatTime,
  formatDate,
  weatherLocation,
  weatherCondition,
  weatherTemp,
  weatherDesc,
  weatherRetryTrigger,
  setWeatherRetryTrigger,
  setWeatherDesc,
  menuOpen,
  setMenuOpen,
  onOpenHistory,
  onOpenQueue,
  onOpenRooms,
  onOpenSettings,
  onOpenHelp,
  onOpenWeatherSettings,
  onOpenWeatherForecast,
  onOpenBugReport,
  onCalendarModalOpenChange,
  userSettings,
  activeAgentCount,
  agentDrawerExpanded,
  onToggleAgentDrawer,
}) => {
  const canOpenWeatherForecast = Boolean(
    weatherLocation
    && weatherTemp !== '--'
    && weatherDesc
    && weatherDesc !== 'Weather unavailable'
    && weatherDesc !== 'Retrying...'
  );
  const handleWeatherClick = (event) => {
    if (weatherDesc === 'Weather unavailable') {
      setWeatherDesc('Retrying...');
      setWeatherRetryTrigger(n => n + 1);
      return;
    }
    if (canOpenWeatherForecast) {
      onOpenWeatherForecast(event.currentTarget.getBoundingClientRect());
    }
  };

  return (
    <div className={`viola-topbar ${styles.topbar}`}>
      {/* Left: Time + Weather */}
      <div className={`viola-topbar-left ${styles.topbarLeft}`}>
        <time
          className={`viola-clock ${styles.clock}`}
          role="time"
          aria-label="Current time"
          dateTime={currentTime.toISOString()}
        >
          {formatTime(currentTime)}
        </time>

        <ErrorBoundary name="Weather">
          {!weatherLocation ? (
            <div
              className={styles.weatherSetup}
              onClick={onOpenWeatherSettings}
              title="Set your weather location in Settings"
            >
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke={THEME.colors.textMuted} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/>
                <circle cx="12" cy="10" r="3"/>
              </svg>
              <span className={styles.weatherSetupText} style={{ color: THEME.colors.textMuted }}>
                Set your location
              </span>
            </div>
          ) : (
            <div
              className={styles.weatherDisplay}
              style={{ cursor: weatherDesc === 'Weather unavailable' || canOpenWeatherForecast ? 'pointer' : 'default' }}
              onClick={handleWeatherClick}
              title={weatherDesc === 'Weather unavailable' ? 'Click to retry' : canOpenWeatherForecast ? 'Open forecast' : undefined}
            >
              <div className={styles.weatherIconWrap}>
                <WeatherIcon condition={weatherCondition} />
              </div>
              <div className={styles.weatherInfo}>
                <span className={styles.weatherTemp}>
                  {weatherTemp}
                </span>
                <span className={styles.weatherDesc} style={{ color: THEME.colors.textMuted }}>
                  {weatherDesc}
                </span>
              </div>
            </div>
          )}
        </ErrorBoundary>
      </div>

      {/* Right: Date + Calendar + Menu */}
      <div className={styles.topbarRight}>
        {/* The nav dropdown menu (below) renders as an overlay that can sit
            over this compact calendar preview at narrower widths (e.g.
            tablet). Hide it from assistive tech and the display-integrity
            scanner (and make it non-interactive) while the menu is open, so
            the menu items don't register as overlapping the calendar's day
            cells -- same pattern as the pill row in SmartDisplay.jsx. */}
        <div
          className={styles.calendarWrap}
          aria-hidden={menuOpen ? 'true' : undefined}
          inert={menuOpen ? '' : undefined}
        >
          <div className={styles.dateText} style={{ color: THEME.colors.textMuted }}>
            {formatDate(currentTime)}
          </div>
          <div className={styles.calendarPanel}>
            <CalendarView
              theme={THEME}
              timeFormat={userSettings.time_display_format || 'auto'}
              compact={true}
              onModalOpenChange={onCalendarModalOpenChange}
            />
          </div>
        </div>
        <div className={styles.topbarActions}>
          {activeAgentCount > 0 && (
            <button
              type="button"
              className={styles.agentCountButton}
              aria-label={agentDrawerExpanded ? 'Collapse background agents' : 'Show background agents'}
              title={agentDrawerExpanded ? 'Collapse background agents' : 'Show background agents'}
              onClick={onToggleAgentDrawer}
            >
              {activeAgentCount}
            </button>
          )}
          <button
            type="button"
            className={styles.bugReportButton}
            aria-label="Report a bug"
            title="Report a bug"
            onClick={onOpenBugReport}
          >
            {/* Lucide "bug" glyph (not alert-triangle -- that path is this app's
                own warning-state icon, see Toast.jsx's LEVEL_ICONS.warning, so
                reusing it here made the hero screen's only bug-report control
                permanently read as "something is wrong" instead of "report a
                bug", see #4788 / triage C-666). */}
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 20v-9" />
              <path d="M14 7a4 4 0 0 1 4 4v3a6 6 0 0 1-12 0v-3a4 4 0 0 1 4-4z" />
              <path d="M14.12 3.88 16 2" />
              <path d="M21 21a4 4 0 0 0-3.81-4" />
              <path d="M21 5a4 4 0 0 1-3.55 3.97" />
              <path d="M22 13h-4" />
              <path d="M3 21a4 4 0 0 1 3.81-4" />
              <path d="M3 5a4 4 0 0 0 3.55 3.97" />
              <path d="M6 13H2" />
              <path d="m8 2 1.88 1.88" />
              <path d="M9 7.13V6a3 3 0 1 1 6 0v1.13" />
            </svg>
          </button>
          <div className={`menu-container ${styles.menuContainer}`}>
            <MenuButton onClick={(e) => { e.stopPropagation(); setMenuOpen(!menuOpen); }} isOpen={menuOpen} />
            <DropdownMenu
              isOpen={menuOpen}
              onClose={() => setMenuOpen(false)}
              onOpenHistory={onOpenHistory}
              onOpenQueue={onOpenQueue}
              onOpenRooms={onOpenRooms}
              onOpenSettings={onOpenSettings}
              onOpenHelp={onOpenHelp}
            />
          </div>
        </div>
      </div>
    </div>
  );
};

TopBar.propTypes = {
  currentTime: PropTypes.instanceOf(Date).isRequired,
  formatTime: PropTypes.func.isRequired,
  formatDate: PropTypes.func.isRequired,
  weatherLocation: PropTypes.string,
  weatherCondition: PropTypes.string.isRequired,
  weatherTemp: PropTypes.string.isRequired,
  weatherDesc: PropTypes.string.isRequired,
  weatherRetryTrigger: PropTypes.number,
  setWeatherRetryTrigger: PropTypes.func,
  setWeatherDesc: PropTypes.func,
  menuOpen: PropTypes.bool.isRequired,
  setMenuOpen: PropTypes.func.isRequired,
  onOpenHistory: PropTypes.func.isRequired,
  onOpenQueue: PropTypes.func.isRequired,
  onOpenRooms: PropTypes.func.isRequired,
  onOpenSettings: PropTypes.func.isRequired,
  onOpenHelp: PropTypes.func.isRequired,
  onOpenWeatherSettings: PropTypes.func.isRequired,
  onOpenWeatherForecast: PropTypes.func,
  onOpenBugReport: PropTypes.func.isRequired,
  onCalendarModalOpenChange: PropTypes.func,
  userSettings: PropTypes.object.isRequired,
  activeAgentCount: PropTypes.number,
  agentDrawerExpanded: PropTypes.bool,
  onToggleAgentDrawer: PropTypes.func,
};

TopBar.defaultProps = {
  activeAgentCount: 0,
  agentDrawerExpanded: true,
  onToggleAgentDrawer: () => {},
  onOpenWeatherForecast: () => {},
};

export default TopBar;
