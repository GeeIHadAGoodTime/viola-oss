import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { generateQR } from '../../utils/qrcode';
import {
  CHANNEL_PAIRING_CONFIGS,
  PAIRING_PHASES,
  useChannelPairing,
} from './useChannelPairing';

const theme = THEME;
const QR_SIZE = 200;

function buildQrModules(deepLink) {
  if (!deepLink) return null;
  try {
    return generateQR(deepLink);
  } catch {
    return null;
  }
}

function QRCodeSVG({ modules, size = QR_SIZE, label }) {
  if (!modules || modules.length === 0) return null;

  const count = modules.length;
  const cellSize = size / count;
  const paths = [];
  for (let row = 0; row < count; row++) {
    for (let column = 0; column < count; column++) {
      if (modules[row][column]) {
        paths.push(`M${column * cellSize},${row * cellSize}h${cellSize}v${cellSize}h-${cellSize}z`);
      }
    }
  }

  return (
    <svg
      role="img"
      aria-label={label}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ display: 'block', background: '#ffffff' }}
    >
      <path d={paths.join('')} fill="#000000" />
    </svg>
  );
}

function formatRemaining(seconds) {
  const safeSeconds = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(safeSeconds / 60);
  const remainder = safeSeconds % 60;
  return `${minutes}:${String(remainder).padStart(2, '0')}`;
}

function formatLinkedAt(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `Connected since ${date.toLocaleDateString()}`;
}

QRCodeSVG.propTypes = {
  label: PropTypes.string.isRequired,
  modules: PropTypes.arrayOf(PropTypes.arrayOf(PropTypes.bool)),
  size: PropTypes.number,
};

function StatusBadge({ children }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 8px',
        borderRadius: '999px',
        backgroundColor: `${theme.colors.statusGreen}20`,
        color: theme.colors.statusGreen,
        fontSize: '12px',
        fontWeight: 600,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: '6px',
          height: '6px',
          borderRadius: '50%',
          backgroundColor: theme.colors.statusGreen,
        }}
      />
      {children}
    </span>
  );
}

StatusBadge.propTypes = {
  children: PropTypes.node.isRequired,
};

function PrimaryButton({ children, disabled, onClick }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      style={{
        padding: '10px 16px',
        minHeight: '44px',
        borderRadius: '8px',
        border: 'none',
        backgroundColor: theme.colors.accent,
        color: '#ffffff',
        cursor: disabled ? 'not-allowed' : 'pointer',
        fontSize: '14px',
        fontWeight: 600,
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {children}
    </button>
  );
}

PrimaryButton.propTypes = {
  children: PropTypes.node.isRequired,
  disabled: PropTypes.bool,
  onClick: PropTypes.func.isRequired,
};

function SecondaryButton({ children, disabled, onClick, danger = false }) {
  const color = danger ? theme.colors.statusRed : theme.colors.textSecondary;
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      style={{
        padding: '10px 16px',
        minHeight: '44px',
        borderRadius: '8px',
        border: `1px solid ${danger ? theme.colors.statusRed : theme.colors.borderLight}`,
        backgroundColor: 'transparent',
        color,
        cursor: disabled ? 'not-allowed' : 'pointer',
        fontSize: '14px',
        fontWeight: 500,
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {children}
    </button>
  );
}

SecondaryButton.propTypes = {
  children: PropTypes.node.isRequired,
  danger: PropTypes.bool,
  disabled: PropTypes.bool,
  onClick: PropTypes.func.isRequired,
};

function ErrorMessage({ message }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      style={{
        padding: '10px 12px',
        borderRadius: '8px',
        backgroundColor: `${theme.colors.statusRed}14`,
        border: `1px solid ${theme.colors.statusRed}50`,
        color: theme.colors.statusRed,
        fontSize: '13px',
        lineHeight: 1.4,
      }}
    >
      {message}
    </div>
  );
}

ErrorMessage.propTypes = {
  message: PropTypes.string,
};

function QRPairCardContent({ config }) {
  const pairing = useChannelPairing(config);

  const titleId = `qr-pair-${config.id}-title`;
  const isChecking = pairing.phase === PAIRING_PHASES.CHECKING;
  const isPairing = pairing.phase === PAIRING_PHASES.PAIRING;
  const isLinked = pairing.phase === PAIRING_PHASES.LINKED;
  const accountLabel = config.getAccountLabel(pairing.status);
  const linkedMeta = formatLinkedAt(pairing.status?.linked_at);
  const qrModules = pairing.linkToken?.deepLink ? buildQrModules(pairing.linkToken.deepLink) : null;

  const handleUnlink = () => {
    if (window.confirm('Disconnect Telegram? Your conversation history will be deleted.')) {
      void pairing.unlink();
    }
  };

  return (
    <section
      aria-busy={isChecking || pairing.isBusy}
      aria-labelledby={titleId}
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: '14px',
        padding: '18px',
        borderRadius: '8px',
        border: `1px solid ${theme.colors.borderSubtle}`,
        backgroundColor: theme.colors.bgElevated,
        color: theme.colors.textPrimary,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'flex-start' }}>
        <div>
          <h3
            id={titleId}
            style={{
              margin: 0,
              color: theme.colors.textBright,
              fontSize: '16px',
              fontWeight: 650,
            }}
          >
            {config.name}
          </h3>
          <p
            style={{
              margin: '5px 0 0',
              color: theme.colors.textMuted,
              fontSize: '13px',
              lineHeight: 1.4,
            }}
          >
            Text Viola from your phone via Telegram.
          </p>
        </div>
        {isLinked && <StatusBadge>Connected</StatusBadge>}
      </div>

      <ErrorMessage message={pairing.error} />

      {isChecking && (
        <div role="status" aria-live="polite" style={{ color: theme.colors.textMuted, fontSize: '13px' }}>
          Checking Telegram connection...
        </div>
      )}

      {pairing.phase === PAIRING_PHASES.UNLINKED && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', alignItems: 'flex-start' }}>
          <p style={{ margin: 0, color: theme.colors.textSecondary, fontSize: '13px', lineHeight: 1.45 }}>
            Connect your account to get started.
          </p>
          <PrimaryButton disabled={pairing.busyAction === 'pairing'} onClick={pairing.startPairing}>
            {pairing.busyAction === 'pairing' ? 'Generating link...' : 'Pair with Telegram'}
          </PrimaryButton>
        </div>
      )}

      {isPairing && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', alignItems: 'flex-start' }}>
          {qrModules && (
            <div
              style={{
                padding: '10px',
                borderRadius: '8px',
                backgroundColor: '#ffffff',
                border: `1px solid ${theme.colors.borderLight}`,
              }}
            >
              <QRCodeSVG modules={qrModules} label="Scan to pair Telegram with Viola" />
            </div>
          )}
          {pairing.linkToken?.deepLink && (
            <>
              <p style={{ margin: 0, color: theme.colors.textSecondary, fontSize: '13px' }}>
                Or tap this link on your phone:
              </p>
              <a
                href={pairing.linkToken.deepLink}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  color: theme.colors.textBright,
                  fontSize: '13px',
                  lineHeight: 1.5,
                  overflowWrap: 'anywhere',
                }}
              >
                {pairing.linkToken.deepLink}
              </a>
            </>
          )}
          <div
            role="status"
            aria-live="polite"
            style={{ color: theme.colors.textMuted, fontSize: '12px', lineHeight: 1.4 }}
          >
            Link expires in {formatRemaining(pairing.remainingSeconds)}
          </div>
          <SecondaryButton disabled={pairing.isBusy} onClick={pairing.cancelPairing}>
            Cancel
          </SecondaryButton>
        </div>
      )}

      {isLinked && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', alignItems: 'flex-start' }}>
          <div>
            <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 600 }}>
              {accountLabel}
            </div>
            {linkedMeta && (
              <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '4px' }}>
                {linkedMeta}
              </div>
            )}
          </div>
          <SecondaryButton danger disabled={pairing.busyAction === 'unlink'} onClick={handleUnlink}>
            {pairing.busyAction === 'unlink' ? 'Disconnecting...' : 'Unpair'}
          </SecondaryButton>
        </div>
      )}
    </section>
  );
}

QRPairCardContent.propTypes = {
  config: PropTypes.shape({
    id: PropTypes.string.isRequired,
    name: PropTypes.string.isRequired,
    getAccountLabel: PropTypes.func.isRequired,
  }).isRequired,
};

export default function QRPairCard({ channel = 'telegram' }) {
  const config = CHANNEL_PAIRING_CONFIGS[channel];

  if (!config) {
    return (
      <section role="alert" style={{ color: theme.colors.statusRed }}>
        Unsupported messaging channel.
      </section>
    );
  }

  return <QRPairCardContent config={config} />;
}

QRPairCard.propTypes = {
  channel: PropTypes.oneOf(Object.keys(CHANNEL_PAIRING_CONFIGS)),
};

export { buildQrModules, formatRemaining };
