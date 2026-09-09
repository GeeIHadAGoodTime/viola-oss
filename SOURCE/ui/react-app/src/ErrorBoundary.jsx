import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from './config';

/**
 * Error Boundary for Viola React App
 * Catches JavaScript errors in child components and displays fallback UI.
 * Prevents white-screen crashes that require app restart.
 */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error) {
    // Update state so next render shows fallback UI
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    // Store error info for display
    this.setState({ errorInfo });

    // Only log to console in development mode
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.error('React Error Boundary caught an error:', error, errorInfo);
    }
  }

  handleReload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div style={{
          padding: '40px',
          textAlign: 'center',
          backgroundColor: THEME.colors.bgElevated,
          color: THEME.colors.textPrimary,
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: 'system-ui, -apple-system, sans-serif'
        }}>
          <h2 style={{ marginBottom: '16px', color: THEME.colors.statusRed }}>
            Something went wrong
          </h2>
          <p style={{ marginBottom: '8px', color: THEME.colors.textMuted, maxWidth: '400px' }}>
            The application encountered an unexpected error.
          </p>
          <p style={{ marginBottom: '24px', color: THEME.colors.textMuted, maxWidth: '400px' }}>
            Please reload the app to continue.
          </p>
          <button
            onClick={this.handleReload}
            style={{
              padding: '12px 24px',
              fontSize: '16px',
              backgroundColor: THEME.colors.accent,
              color: THEME.colors.textBright,
              border: 'none',
              borderRadius: '8px',
              cursor: 'pointer',
              transition: 'background-color 0.2s'
            }}
            onMouseOver={(e) => e.target.style.opacity = '0.85'}
            onMouseOut={(e) => e.target.style.opacity = '1'}
          >
            Reload Application
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

ErrorBoundary.propTypes = {
  children: PropTypes.node.isRequired,
};

export default ErrorBoundary;
