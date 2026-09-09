import React from 'react';
import PropTypes from 'prop-types';

const RETRY_DELAY_MS = 30000;

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
    this._retryTimer = null;
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error(
      `[ErrorBoundary] ${this.props.name || 'Widget'} crashed:`,
      error,
      errorInfo?.componentStack,
    );
  }

  componentWillUnmount() {
    if (this._retryTimer) clearTimeout(this._retryTimer);
  }

  _scheduleRetry() {
    if (this._retryTimer) return;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      this.setState({ hasError: false, error: null });
    }, RETRY_DELAY_MS);
  }

  _handleRetryClick = () => {
    if (this._retryTimer) clearTimeout(this._retryTimer);
    this._retryTimer = null;
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      this._scheduleRetry();

      return (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '8px',
            padding: '8px 12px',
            color: 'rgba(255,255,255,0.35)',
            fontSize: '13px',
            minHeight: '2em',
          }}
        >
          <span>{this.props.name || 'Widget'} couldn't load</span>
          <button
            onClick={this._handleRetryClick}
            style={{
              background: 'none',
              border: '1px solid rgba(255,255,255,0.15)',
              borderRadius: '4px',
              color: 'rgba(255,255,255,0.4)',
              fontSize: '11px',
              minHeight: '44px',
              padding: '2px 12px',
              cursor: 'pointer',
            }}
          >
            Try again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

ErrorBoundary.propTypes = {
  name: PropTypes.string,
  children: PropTypes.node.isRequired,
};

export default ErrorBoundary;
