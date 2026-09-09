import React, { useCallback, useEffect, useId, useRef } from 'react';
import PropTypes from 'prop-types';
import { useTranslation } from 'react-i18next';
import { THEME } from '../config';
import OnboardingSkip from './OnboardingSkip';
import '../i18n';
import '../styles/onboarding.css';

// Warm bronze tones used only by the onboarding surface. THEME.colors.accent is
// the mahogany brand color (#6B2E1B); these lighter embers give the dark glass
// its lamplit edge without polluting the global palette.
const BRONZE = '#c98a68';
const BRONZE_DEEP = '#7d3a22';
const EMBER_GLOW = 'rgba(201, 138, 104, 0.55)';

// The one-time "How did you hear about Viola?" choices. Values MUST stay in
// sync with the server-side funnel enum (FUNNEL_ATTRIBUTION_OPTIONS in
// admin/metrics_backends/protocol.py) and the attribution_self_report enum
// allowlist in ui/settings_api.py. The answer is optional and non-identifying;
// "prefer not to say" stores "" (the default).
export const ATTRIBUTION_OPTIONS = [
  { value: 'reddit', labelKey: 'onboarding.attribution.options.reddit' },
  { value: 'x', labelKey: 'onboarding.attribution.options.x' },
  { value: 'youtube', labelKey: 'onboarding.attribution.options.youtube' },
  { value: 'tiktok', labelKey: 'onboarding.attribution.options.tiktok' },
  { value: 'hn', labelKey: 'onboarding.attribution.options.hn' },
  { value: 'search', labelKey: 'onboarding.attribution.options.search' },
  { value: 'friend', labelKey: 'onboarding.attribution.options.friend' },
  { value: 'blog', labelKey: 'onboarding.attribution.options.blog' },
  { value: 'other', labelKey: 'onboarding.attribution.options.other' },
];

export default function OnboardingOverlay({
  isOnboarding,
  phase,
  phaseIndex,
  totalPhases,
  phaseContent,
  isSpeaking,
  suggestions,
  isWelcomePhase,
  onWelcomeContinue,
  onReportBug,
  isAccountPairPhase,
  signInStatus,
  accountSkipped,
  onContinueWithoutAccount,
  onReturnToSignIn,
  isMicPermissionPhase,
  micPermission,
  isCloudConsentPhase,
  cloudConsentStatus,
  cloudConsentError,
  isAutonomyPhase,
  autonomyStatus,
  autonomyError,
  isAttributionPhase,
  attributionStatus,
  tryCommandStatus,
  skipOnboarding,
  onSuggestionTap,
  onMicPermissionRetry,
  onSkipMicStep,
  onCloudConsentChoice,
  onByokSetupDone,
  onAutonomyChoice,
  onAttributionChoice,
  sendCommand,
  onOpenSettings,
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const descriptionId = useId();
  const progressId = useId();
  const rootRef = useRef(null);
  const cardRef = useRef(null);
  const phaseTitle = phase ? t(`onboarding.phases.${phase}.title`) : t('onboarding.a11y.phase_title');

  const handleSuggestionClick = useCallback((text) => {
    if (onSuggestionTap && sendCommand) {
      onSuggestionTap(text, sendCommand);
    }
  }, [onSuggestionTap, sendCommand]);

  // Keyboard focus management: move focus into the dialog when it opens / the
  // phase changes, and trap Tab within the dialog so keyboard users cannot land
  // on the (blurred, inert) app behind the modal. role=dialog + aria-modal
  // already announce the modal; this makes the focus behaviour match.
  //
  // The trap spans the whole dialog root, not just the card: the dismiss
  // control is a fixed-position sibling of the card, and it is the one exit
  // from first run, so a card-only trap made the exit unreachable by keyboard.
  useEffect(() => {
    if (!isOnboarding) return undefined;
    const root = rootRef.current;
    const card = cardRef.current;
    if (!root || !card) return undefined;

    const focusablesSelector =
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    // Initial focus: first actionable control in the card, else the card itself.
    const first = card.querySelector(focusablesSelector);
    if (first) {
      first.focus();
    } else {
      card.focus();
    }

    const onKeyDown = (e) => {
      if (e.key !== 'Tab') return;
      const focusables = Array.from(root.querySelectorAll(focusablesSelector)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (focusables.length === 0) {
        e.preventDefault();
        card.focus();
        return;
      }
      const firstEl = focusables[0];
      const lastEl = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    root.addEventListener('keydown', onKeyDown);
    return () => root.removeEventListener('keydown', onKeyDown);
  }, [isOnboarding, phase]);

  if (!isOnboarding) return null;

  return (
    <div
      className="viola-onboard-backdrop"
      ref={rootRef}
      style={{
        position: 'fixed',
        inset: 0,
        // Deep dim with a warm mahogany glow rising from below (lamplight on
        // wood), plus a heavy blur so the running app fully dissolves behind
        // the card. pointerEvents stay off the backdrop so the highlighted
        // push-to-talk button underneath remains reachable during the mic
        // and try-a-command phases.
        background:
          'radial-gradient(130% 105% at 50% 118%, rgba(107,46,27,0.30) 0%, rgba(107,46,27,0.12) 32%, rgba(0,0,0,0.86) 68%)',
        backdropFilter: 'blur(30px) saturate(118%)',
        WebkitBackdropFilter: 'blur(30px) saturate(118%)',
        zIndex: 90,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        padding: '24px',
        pointerEvents: 'none',
      }}
      data-testid="onboarding-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t('onboarding.a11y.dialog_label')}
      aria-labelledby={titleId}
      aria-describedby={`${descriptionId} ${progressId}`}
    >
      <div
        className="viola-onboard-card"
        ref={cardRef}
        tabIndex={-1}
        style={{
          position: 'relative',
          width: 'min(92vw, 540px)',
          maxHeight: '88vh',
          overflowY: 'auto',
          boxSizing: 'border-box',
          padding: '34px 36px 30px',
          borderRadius: '22px',
          background:
            'linear-gradient(158deg, rgba(32,20,15,0.94) 0%, rgba(17,12,10,0.95) 60%, rgba(12,9,8,0.96) 100%)',
          border: '1px solid rgba(201,138,104,0.26)',
          boxShadow:
            '0 30px 90px rgba(0,0,0,0.62), 0 0 70px rgba(107,46,27,0.16), inset 0 1px 0 rgba(255,255,255,0.05)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: '18px',
          textAlign: 'center',
          pointerEvents: 'auto',
        }}
      >
        {/* Eyebrow: the welcome step reads "WELCOME · BETA"; every later step
            carries the persistent "Viola" wordmark (deliberately untranslated).
            This keeps the label consistent across the flow. */}
        <BrandRow
          showBeta={isWelcomePhase}
          betaLabel={t('onboarding.welcome.beta_badge')}
          eyebrow={isWelcomePhase ? t('onboarding.welcome.eyebrow') : 'Viola'}
        />

        <h2
          id={titleId}
          className="viola-onboard-serif"
          style={{
            margin: 0,
            fontSize: 'clamp(28px, 4.2vw, 40px)',
            lineHeight: 1.12,
            color: THEME.colors.textBright,
            maxWidth: '440px',
          }}
        >
          {phaseTitle}
        </h2>

        <div
          id={descriptionId}
          role="status"
          aria-live="polite"
          aria-atomic="true"
          style={{
            fontSize: 'clamp(14px, 1.8vw, 16px)',
            color: THEME.colors.textSecondary,
            lineHeight: 1.6,
            maxWidth: '432px',
            opacity: isSpeaking ? 1 : 0.92,
            transition: 'opacity 0.3s ease',
          }}
        >
          {phaseContent}
        </div>

        {isWelcomePhase && (
          <WelcomePanel onBegin={onWelcomeContinue} onReportBug={onReportBug} />
        )}

        {isAccountPairPhase && (
          <AccountSignInPanel
            status={signInStatus}
            onOpenSettings={onOpenSettings}
            onContinueWithoutAccount={onContinueWithoutAccount}
          />
        )}

        {isMicPermissionPhase && (
          <MicPermissionPanel
            micPermission={micPermission}
            onRetry={onMicPermissionRetry}
            onSkipMicStep={onSkipMicStep}
          />
        )}

        {isCloudConsentPhase && (
          <CloudConsentPanel
            status={cloudConsentStatus}
            error={cloudConsentError}
            onChoice={onCloudConsentChoice}
            onByokSetupDone={onByokSetupDone}
            onOpenSettings={onOpenSettings}
            accountSkipped={accountSkipped}
            onReturnToSignIn={onReturnToSignIn}
          />
        )}

        {isAutonomyPhase && (
          <AutonomyTierPanel
            status={autonomyStatus}
            error={autonomyError}
            onChoice={onAutonomyChoice}
          />
        )}

        {isAttributionPhase && (
          <AttributionPanel
            status={attributionStatus}
            onChoice={onAttributionChoice}
          />
        )}

        {suggestions && (
          <div
            style={{
              display: 'flex',
              gap: '10px',
              flexWrap: 'wrap',
              justifyContent: 'center',
            }}
          >
            {suggestions.map((s) => (
              <SuggestionChip
                key={s.text}
                label={tryCommandStatus === 'running' ? t('onboarding.mic_try.running') : s.label}
                disabled={tryCommandStatus === 'running'}
                onClick={() => handleSuggestionClick(s.text)}
              />
            ))}
          </div>
        )}

        <ProgressRail current={phaseIndex} total={totalPhases} progressId={progressId} />
      </div>

      {/* Always available, on every phase. First run is a guided setup, not a
          gate: a user who cannot finish a step (no account, denied microphone)
          must still be able to reach the app (#4225). */}
      <div style={{ pointerEvents: 'auto' }}>
        <OnboardingSkip visible={true} onSkip={skipOnboarding} />
      </div>
    </div>
  );
}

OnboardingOverlay.propTypes = {
  isOnboarding: PropTypes.bool.isRequired,
  phase: PropTypes.string,
  phaseIndex: PropTypes.number,
  totalPhases: PropTypes.number,
  phaseContent: PropTypes.string,
  isSpeaking: PropTypes.bool,
  suggestions: PropTypes.array,
  isWelcomePhase: PropTypes.bool,
  onWelcomeContinue: PropTypes.func,
  onReportBug: PropTypes.func,
  isAccountPairPhase: PropTypes.bool,
  signInStatus: PropTypes.string,
  accountSkipped: PropTypes.bool,
  onContinueWithoutAccount: PropTypes.func,
  onReturnToSignIn: PropTypes.func,
  isMicPermissionPhase: PropTypes.bool,
  micPermission: PropTypes.shape({
    checking: PropTypes.bool,
    granted: PropTypes.bool,
    blocked: PropTypes.bool,
    message: PropTypes.string,
  }),
  isCloudConsentPhase: PropTypes.bool,
  cloudConsentStatus: PropTypes.string,
  cloudConsentError: PropTypes.string,
  isAutonomyPhase: PropTypes.bool,
  autonomyStatus: PropTypes.string,
  autonomyError: PropTypes.string,
  isAttributionPhase: PropTypes.bool,
  attributionStatus: PropTypes.string,
  tryCommandStatus: PropTypes.string,
  skipOnboarding: PropTypes.func.isRequired,
  onSuggestionTap: PropTypes.func,
  onMicPermissionRetry: PropTypes.func,
  onSkipMicStep: PropTypes.func,
  onCloudConsentChoice: PropTypes.func,
  onByokSetupDone: PropTypes.func,
  onAutonomyChoice: PropTypes.func,
  onAttributionChoice: PropTypes.func,
  sendCommand: PropTypes.func,
  onOpenSettings: PropTypes.func,
};

function BrandRow({ showBeta, betaLabel, eyebrow }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minHeight: '20px' }}>
      <span
        className="viola-onboard-ember"
        aria-hidden="true"
        style={{
          width: '8px',
          height: '8px',
          borderRadius: '50%',
          background: `radial-gradient(circle at 40% 35%, ${BRONZE}, ${BRONZE_DEEP})`,
          boxShadow: `0 0 10px 2px ${EMBER_GLOW}`,
        }}
      />
      <span
        style={{
          fontSize: '12px',
          fontWeight: 600,
          letterSpacing: '3px',
          textTransform: 'uppercase',
          color: BRONZE,
        }}
      >
        {eyebrow}
      </span>
      {showBeta && (
        <span
          style={{
            fontSize: '10px',
            fontWeight: 700,
            letterSpacing: '1.5px',
            textTransform: 'uppercase',
            color: '#f3d9cb',
            padding: '3px 8px',
            borderRadius: '999px',
            border: '1px solid rgba(201,138,104,0.45)',
            background: 'rgba(107,46,27,0.35)',
          }}
        >
          {betaLabel}
        </span>
      )}
    </div>
  );
}

BrandRow.propTypes = {
  showBeta: PropTypes.bool,
  betaLabel: PropTypes.string,
  eyebrow: PropTypes.string,
};

function WelcomePanel({ onBegin, onReportBug }) {
  const { t } = useTranslation();

  return (
    <Panel>
      <MutedText>{t('onboarding.welcome.beta_note')}</MutedText>
      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
        <ActionButton label={t('onboarding.welcome.begin')} onClick={onBegin} />
        {onReportBug && (
          <ActionButton
            label={t('onboarding.welcome.report_bug')}
            variant="secondary"
            onClick={onReportBug}
          />
        )}
      </div>
      <MutedText>{t('onboarding.welcome.report_hint')}</MutedText>
    </Panel>
  );
}

WelcomePanel.propTypes = {
  onBegin: PropTypes.func,
  onReportBug: PropTypes.func,
};

function AccountSignInPanel({ status, onOpenSettings, onContinueWithoutAccount }) {
  const { t } = useTranslation();
  const handleOpenAccountSettings = useCallback(() => {
    if (onOpenSettings) onOpenSettings('account');
  }, [onOpenSettings]);

  if (status === 'signed_in') {
    return (
      <Panel>
        <StatusText>{t('onboarding.account_pair.signed_in')}</StatusText>
      </Panel>
    );
  }

  // Signing in stays the primary, recommended action: it is what unlocks the
  // free managed tier most people want. The secondary action is the honest
  // alternative for everyone else, because Viola genuinely runs without an
  // account on your own provider key or a local model.
  return (
    <Panel>
      <MutedText>
        {status === 'checking'
          ? t('onboarding.account_pair.preparing')
          : t('onboarding.account_pair.instructions')}
      </MutedText>
      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
        <ActionButton
          label={t('onboarding.account_pair.open_settings')}
          onClick={handleOpenAccountSettings}
        />
        <ActionButton
          label={t('onboarding.account_pair.no_account')}
          variant="secondary"
          onClick={onContinueWithoutAccount}
        />
      </div>
      <MutedText>{t('onboarding.account_pair.no_account_hint')}</MutedText>
    </Panel>
  );
}

AccountSignInPanel.propTypes = {
  status: PropTypes.string,
  onOpenSettings: PropTypes.func,
  onContinueWithoutAccount: PropTypes.func,
};

function MicPermissionPanel({ micPermission, onRetry, onSkipMicStep }) {
  const { t } = useTranslation();
  if (!micPermission?.checking && !micPermission?.blocked) return null;

  return (
    <Panel>
      {micPermission.checking ? (
        <MutedText>{t('onboarding.mic.checking')}</MutedText>
      ) : (
        <>
          <ErrorText>{micPermission.message || t('onboarding.mic.unknown')}</ErrorText>
          <ActionButton label={t('common.actions.check_again')} onClick={onRetry} />
          {/* Retry alone made a wrong "blocked" verdict a dead end. */}
          {onSkipMicStep && (
            <ActionButton
              label={t('onboarding.mic.continue_without')}
              onClick={onSkipMicStep}
            />
          )}
        </>
      )}
    </Panel>
  );
}

MicPermissionPanel.propTypes = {
  micPermission: PropTypes.shape({
    checking: PropTypes.bool,
    blocked: PropTypes.bool,
    message: PropTypes.string,
  }),
  onRetry: PropTypes.func,
  onSkipMicStep: PropTypes.func,
};

function CloudConsentPanel({
  status,
  error,
  onChoice,
  onByokSetupDone,
  onOpenSettings,
  accountSkipped,
  onReturnToSignIn,
}) {
  const { t } = useTranslation();
  const handleOpenAiSettings = useCallback(() => {
    if (onOpenSettings) onOpenSettings('ai_agents');
  }, [onOpenSettings]);

  if (status === 'declined') {
    return (
      <Panel>
        <MutedText>{t('onboarding.cloud.settings_needed')}</MutedText>
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
          <ActionButton label={t('onboarding.cloud.open_settings')} onClick={handleOpenAiSettings} />
          <ActionButton label={t('onboarding.cloud.added_key')} variant="secondary" onClick={onByokSetupDone} />
        </div>
      </Panel>
    );
  }

  // Continuing without an account: Viola-managed AI is metered per account, so
  // offering an "enable" button here would hand the user a control that can
  // only fail at the account gate later. Show the paths that actually work,
  // and keep the door back to sign-in open.
  if (accountSkipped) {
    return (
      <Panel>
        <MutedText>{t('onboarding.cloud.no_account_notice')}</MutedText>
        {error ? <ErrorText>{error}</ErrorText> : null}
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
          <ActionButton
            label={status === 'saving' ? t('common.actions.saving') : t('onboarding.cloud.use_own_key')}
            disabled={status === 'saving'}
            onClick={() => onChoice && onChoice('decline')}
          />
          <ActionButton
            label={t('onboarding.cloud.sign_in_instead')}
            variant="secondary"
            disabled={status === 'saving'}
            onClick={onReturnToSignIn}
          />
        </div>
      </Panel>
    );
  }

  return (
    <Panel>
      <MutedText>
        {t('onboarding.cloud.managed_notice')}
      </MutedText>
      {error ? <ErrorText>{error}</ErrorText> : null}
      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
        <ActionButton
          label={status === 'saving' ? t('common.actions.saving') : t('onboarding.cloud.enable')}
          disabled={status === 'saving'}
          onClick={() => onChoice && onChoice('enable')}
        />
        <ActionButton
          label={t('onboarding.cloud.use_own_key')}
          variant="secondary"
          disabled={status === 'saving'}
          onClick={() => onChoice && onChoice('decline')}
        />
      </div>
    </Panel>
  );
}

CloudConsentPanel.propTypes = {
  status: PropTypes.string,
  error: PropTypes.string,
  onChoice: PropTypes.func,
  onByokSetupDone: PropTypes.func,
  onOpenSettings: PropTypes.func,
  accountSkipped: PropTypes.bool,
  onReturnToSignIn: PropTypes.func,
};

function AttributionPanel({ status, onChoice }) {
  const { t } = useTranslation();
  const saving = status === 'saving';

  return (
    <Panel>
      <MutedText>{t('onboarding.attribution.optional_note')}</MutedText>
      <div
        role="group"
        aria-label={t('onboarding.attribution.group_label')}
        style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center' }}
      >
        {ATTRIBUTION_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            className="viola-onboard-btn"
            data-testid={`attribution-${option.value}`}
            disabled={saving}
            onClick={() => onChoice && onChoice(option.value)}
            onMouseOver={(e) => { if (!saving) e.currentTarget.style.borderColor = 'rgba(201,138,104,0.6)'; }}
            onMouseOut={(e) => { e.currentTarget.style.borderColor = 'rgba(201,138,104,0.22)'; }}
            style={{
              padding: '8px 14px',
              fontSize: '13px',
              color: saving ? THEME.colors.textMuted : THEME.colors.textPrimary,
              backgroundColor: 'rgba(255,255,255,0.045)',
              border: '1px solid rgba(201,138,104,0.22)',
              borderRadius: '999px',
              cursor: saving ? 'default' : 'pointer',
              whiteSpace: 'nowrap',
            }}
          >
            {t(option.labelKey)}
          </button>
        ))}
      </div>
      <ActionButton
        label={saving ? t('common.actions.saving') : t('onboarding.attribution.prefer_not')}
        variant="secondary"
        disabled={saving}
        onClick={() => onChoice && onChoice('')}
      />
    </Panel>
  );
}

AttributionPanel.propTypes = {
  status: PropTypes.string,
  onChoice: PropTypes.func,
};

// Autonomy tiers — the agent's trust/permission level. Copy mirrors the
// SettingsModal tier cards so onboarding and Settings say the same thing.
// All three are presented neutrally; onboarding recommends none and pre-selects
// none, so the user makes the trust call themselves. Order is least → most access.
const AUTONOMY_TIER_IDS = ['solo', 'ensemble', 'symphony'];

function AutonomyTierPanel({ status, error, onChoice }) {
  const { t } = useTranslation();
  const saving = status === 'saving';

  return (
    <Panel>
      <MutedText>{t('onboarding.autonomy.note')}</MutedText>
      <div
        role="radiogroup"
        aria-label={t('onboarding.autonomy.group_label')}
        style={{ display: 'flex', flexDirection: 'column', gap: '10px', width: '100%' }}
      >
        {AUTONOMY_TIER_IDS.map((tier) => {
          return (
            <button
              key={tier}
              type="button"
              role="radio"
              aria-checked={false}
              data-testid={`autonomy-${tier}`}
              disabled={saving}
              onClick={() => onChoice && onChoice(tier)}
              onMouseOver={(e) => { if (!saving) e.currentTarget.style.borderColor = 'rgba(201,138,104,0.6)'; }}
              onMouseOut={(e) => { e.currentTarget.style.borderColor = 'rgba(255,255,255,0.10)'; }}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'flex-start',
                gap: '4px',
                textAlign: 'left',
                width: '100%',
                padding: '13px 15px',
                borderRadius: '12px',
                cursor: saving ? 'default' : 'pointer',
                color: THEME.colors.textPrimary,
                backgroundColor: 'rgba(255,255,255,0.035)',
                border: '1px solid rgba(255,255,255,0.10)',
                opacity: saving ? 0.6 : 1,
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '14px', fontWeight: 600, color: '#f3e6df' }}>
                  {t(`onboarding.autonomy.tiers.${tier}.label`)}
                </span>
                <span style={{ fontSize: '11px', color: BRONZE, letterSpacing: '0.3px' }}>
                  {t(`onboarding.autonomy.tiers.${tier}.subtitle`)}
                </span>
              </span>
              <span style={{ fontSize: '12px', color: THEME.colors.textTertiary, lineHeight: 1.45 }}>
                {t(`onboarding.autonomy.tiers.${tier}.description`)}
              </span>
            </button>
          );
        })}
      </div>
      <MutedText>{t('onboarding.autonomy.change_later')}</MutedText>
      {error ? <ErrorText>{error}</ErrorText> : null}
    </Panel>
  );
}

AutonomyTierPanel.propTypes = {
  status: PropTypes.string,
  error: PropTypes.string,
  onChoice: PropTypes.func,
};

function SuggestionChip({ label, disabled, onClick }) {
  const { t } = useTranslation();

  return (
    <button
      type="button"
      className="viola-onboard-btn"
      onClick={onClick}
      disabled={disabled}
      aria-label={t('onboarding.a11y.suggestion_label', { label })}
      onMouseOver={(e) => { if (!disabled) e.currentTarget.style.borderColor = 'rgba(201,138,104,0.6)'; }}
      onMouseOut={(e) => { e.currentTarget.style.borderColor = 'rgba(201,138,104,0.28)'; }}
      style={{
        padding: '10px 20px',
        fontSize: '14px',
        color: disabled ? THEME.colors.textMuted : '#f3e6df',
        backgroundColor: 'rgba(107,46,27,0.22)',
        border: '1px solid rgba(201,138,104,0.28)',
        borderRadius: '999px',
        cursor: disabled ? 'default' : 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </button>
  );
}

SuggestionChip.propTypes = {
  label: PropTypes.string.isRequired,
  disabled: PropTypes.bool,
  onClick: PropTypes.func.isRequired,
};

function Panel({ children }) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      gap: '14px',
      width: '100%',
      maxWidth: '440px',
      marginTop: '2px',
      padding: '18px',
      borderRadius: '14px',
      backgroundColor: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.06)',
    }}>
      {children}
    </div>
  );
}

Panel.propTypes = {
  children: PropTypes.node,
};

function MutedText({ children }) {
  return (
    <div role="status" aria-live="polite" style={{
      fontSize: '13px',
      color: THEME.colors.textTertiary,
      textAlign: 'center',
      lineHeight: 1.5,
    }}>
      {children}
    </div>
  );
}

MutedText.propTypes = {
  children: PropTypes.node,
};

function StatusText({ children }) {
  return (
    <div role="status" aria-live="polite" style={{
      color: BRONZE,
      fontSize: '15px',
      fontWeight: 600,
      textAlign: 'center',
    }}>
      {children}
    </div>
  );
}

StatusText.propTypes = {
  children: PropTypes.node,
};

function ErrorText({ children }) {
  return (
    <div role="alert" aria-live="polite" style={{
      color: THEME.colors.statusRed || '#ff6b6b',
      fontSize: '13px',
      textAlign: 'center',
    }}>
      {children}
    </div>
  );
}

ErrorText.propTypes = {
  children: PropTypes.node,
};

function ActionButton({ label, onClick, variant = 'primary', disabled = false }) {
  const isPrimary = variant === 'primary';

  return (
    <button
      type="button"
      className="viola-onboard-btn"
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: '11px 22px',
        fontSize: '14px',
        fontWeight: 600,
        letterSpacing: '0.2px',
        color: isPrimary ? '#fdf2ec' : '#e8d8cf',
        background: isPrimary
          ? `linear-gradient(180deg, ${BRONZE_DEEP} 0%, ${THEME.colors.accent} 100%)`
          : 'transparent',
        border: isPrimary
          ? '1px solid rgba(201,138,104,0.5)'
          : '1px solid rgba(201,138,104,0.3)',
        borderRadius: '11px',
        cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.6 : 1,
        boxShadow: isPrimary ? '0 6px 18px rgba(107,46,27,0.4)' : 'none',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </button>
  );
}

ActionButton.propTypes = {
  label: PropTypes.string.isRequired,
  onClick: PropTypes.func,
  variant: PropTypes.oneOf(['primary', 'secondary']),
  disabled: PropTypes.bool,
};

function ProgressRail({ current = 0, total = 1, progressId }) {
  const { t } = useTranslation();
  const currentStep = Math.min(total, Math.max(1, current + 1));

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '8px',
        width: '100%',
        paddingTop: '4px',
      }}
    >
      <div
        id={progressId}
        role="progressbar"
        aria-label={t('onboarding.a11y.progress_label')}
        aria-valuemin={1}
        aria-valuemax={total}
        aria-valuenow={currentStep}
        aria-valuetext={t('onboarding.a11y.progress_text', { current: currentStep, total })}
        style={{
          display: 'flex',
          gap: '6px',
          justifyContent: 'center',
          width: '100%',
          maxWidth: '220px',
        }}
      >
        <span style={screenReaderOnlyStyle}>
          {t('onboarding.a11y.progress_text', { current: currentStep, total })}
        </span>
        {Array.from({ length: total }, (_, i) => {
          const done = i < current;
          const active = i === current;
          return (
            <div
              key={i}
              aria-hidden="true"
              style={{
                flex: 1,
                height: '3px',
                borderRadius: '999px',
                background: active
                  ? `linear-gradient(90deg, ${BRONZE_DEEP}, ${BRONZE})`
                  : done
                    ? 'rgba(201,138,104,0.55)'
                    : 'rgba(255,255,255,0.12)',
                boxShadow: active ? `0 0 8px ${EMBER_GLOW}` : 'none',
                transition: 'background 0.3s ease, box-shadow 0.3s ease',
              }}
            />
          );
        })}
      </div>
      <span
        aria-hidden="true"
        style={{
          fontSize: '11px',
          letterSpacing: '0.4px',
          color: THEME.colors.textMuted,
        }}
      >
        {t('onboarding.a11y.progress_text', { current: currentStep, total })}
      </span>
    </div>
  );
}

ProgressRail.propTypes = {
  current: PropTypes.number,
  total: PropTypes.number,
  progressId: PropTypes.string,
};

const screenReaderOnlyStyle = {
  position: 'absolute',
  width: '1px',
  height: '1px',
  padding: 0,
  margin: '-1px',
  overflow: 'hidden',
  clip: 'rect(0, 0, 0, 0)',
  whiteSpace: 'nowrap',
  border: 0,
};
