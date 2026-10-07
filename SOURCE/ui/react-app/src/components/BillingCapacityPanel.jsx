import { useEffect, useId, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { billingOperationStatus, billingPaymentUrl, useBillingCapacity } from '../hooks/useBillingCapacity';
import './BillingCapacityPanel.css';

export function capacityMoney(cents, currency = 'usd') {
  if (!Number.isSafeInteger(cents)) return 'Unavailable';
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(cents / 100); }
  catch { return 'Unavailable'; }
}

function capacityDate(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : 'Not available';
}

export function capacityInputCents(value) {
  // Do not round an invalid selection up, or accept exponent/fractional input.
  return /^\d+$/.test(value) ? Number(value) * 100 : null;
}

function SummaryRow({ label, children }) {
  return <div className="billing-capacity-row"><dt>{label}</dt><dd>{children}</dd></div>;
}
SummaryRow.propTypes = { label: PropTypes.string.isRequired, children: PropTypes.node.isRequired };

export default function BillingCapacityPanel({ accountId, refreshUser, fallback = null }) {
  const billing = useBillingCapacity({ accountId, refreshUser });
  const { catalog, account, quote, attempt, operation, loading, quoting, changing, error } = billing;
  const id = useId();
  const selector = useRef(null);
  const [expanded, setExpanded] = useState(false);
  const [input, setInput] = useState('20');
  const [interval, setInterval] = useState('month');
  const [temporary, setTemporary] = useState(false);
  const [consent, setConsent] = useState(false);
  const [cancelConsent, setCancelConsent] = useState(false);
  const [, setExpiryTick] = useState(0);
  const initialized = useRef(false);
  const allowed = Array.isArray(catalog?.allowed_monthly_price_cents)
    ? catalog.allowed_monthly_price_cents.filter((amount) => Number.isSafeInteger(amount) && amount > 0) : [];
  const selectedCents = capacityInputCents(input);
  const selectedIndex = allowed.indexOf(selectedCents);
  const selection = catalog?.selections?.find((row) => row.monthly_price_cents === selectedCents);
  const available = catalog?.available === true && account?.available === true && allowed.length > 0;
  const status = billingOperationStatus(operation);
  const pending = Boolean(attempt || account?.pending_change);
  const operationUrl = billingPaymentUrl(operation?.authentication_url || operation?.checkout_url);

  useEffect(() => {
    if (!account || initialized.current) return;
    initialized.current = true;
    setInput(String((account.monthly_price_cents || 2000) / 100));
    setInterval(account.interval === 'year' ? 'year' : 'month');
  }, [account]);

  useEffect(() => {
    if (expanded) selector.current?.focus();
  }, [expanded]);

  const changeSelection = (nextInput, nextInterval = interval, nextTemporary = temporary) => {
    billing.invalidateQuote();
    setConsent(false);
    setInput(nextInput);
    setInterval(nextInterval);
    setTemporary(nextTemporary);
  };
  const openSelector = (lower = false) => {
    const current = allowed.indexOf(account?.monthly_price_cents);
    const next = !account?.monthly_price_cents ? (allowed.includes(2000) ? 2000 : allowed[0])
      : lower ? allowed[Math.max(0, current - 1)] : allowed[Math.min(allowed.length - 1, current + 1)];
    if (next) changeSelection(String(next / 100), account?.interval || 'month', false);
    setExpanded(true);
  };
  const validQuote = quote && typeof quote.quote_id === 'string'
    && ['increase', 'schedule', 'noop', 'checkout'].includes(quote.action)
    && typeof quote.currency === 'string'
    && typeof quote.capacity_multiplier === 'number' && Number.isFinite(quote.capacity_multiplier)
    && typeof quote.additional_current_cycle_multiplier === 'number' && Number.isFinite(quote.additional_current_cycle_multiplier)
    && Number.isSafeInteger(quote.amount_due_now_cents) && quote.amount_due_now_cents >= 0
    && Number.isSafeInteger(quote.next_recurring_amount_cents)
    && quote.target_monthly_price_cents === selectedCents && quote.interval === interval
    && typeof quote.terms_version === 'string' && typeof quote.privacy_version === 'string';
  const quoteExpiry = validQuote ? new Date(quote.expires_at).getTime() : NaN;
  const quoteUsable = validQuote && Number.isFinite(quoteExpiry) && quoteExpiry > Date.now();
  const confirmLabel = quote?.action === 'schedule' ? 'Confirm change at renewal'
    : quote?.action === 'checkout' ? `Continue to payment — ${capacityMoney(quote?.amount_due_now_cents, quote?.currency)} today`
      : quote?.action === 'noop' ? 'Already selected'
        : `Confirm upgrade — ${capacityMoney(quote?.amount_due_now_cents, quote?.currency)} today`;
  const legalRequest = { terms_accepted: true, terms_version: quote?.terms_version, privacy_version: quote?.privacy_version };
  useEffect(() => {
    if (!Number.isFinite(quoteExpiry)) return undefined;
    const timer = setTimeout(() => setExpiryTick((tick) => tick + 1), Math.min(2147483647, Math.max(0, quoteExpiry - Date.now() + 1)));
    return () => clearTimeout(timer);
  }, [quoteExpiry]);
  const colors = THEME.colors;
  const style = { '--billing-bg': colors.bgCard, '--billing-text': colors.textPrimary,
    '--billing-muted': colors.textSecondary, '--billing-border': colors.borderHover,
    '--billing-accent': colors.bronze, '--billing-action': colors.accent, '--billing-error': colors.statusRed };

  // The previous fixed-plan checkout is retained only while the new catalog is
  // disabled or unreachable, so established standalone/older deployments work.
  if (!loading && !available && fallback && !pending && !account?.billing_review_required) return fallback;

  return (
    <section className="billing-capacity" style={style} aria-label="Billing" data-testid="billing-capacity-panel">
      <h3>Managed usage</h3>
      {loading && !account && <p role="status">Loading billing…</p>}
      {account && <>
        <dl className="billing-capacity-summary">
          <SummaryRow label="Current plan">{account.monthly_price_cents > 0
            ? `${capacityMoney(account.recurring_amount_cents ?? (account.interval === 'year'
              ? catalog?.selections?.find((row) => row.monthly_price_cents === account.monthly_price_cents)?.annual_price_cents
              : account.monthly_price_cents))}/${account.interval === 'year' ? 'year' : 'month'} · ${account.capacity_multiplier}× Pro`
            : 'Free'}</SummaryRow>
          {typeof account.usage_percent === 'number' && <SummaryRow label="Managed usage">{account.usage_percent}% used</SummaryRow>}
          <SummaryRow label="Usage resets">{capacityDate(account.usage_resets_at)}</SummaryRow>
          {account.billing_review_required && typeof account.effective_capacity_multiplier === 'number'
            && <SummaryRow label="Available managed capacity">{account.effective_capacity_multiplier}× Pro</SummaryRow>}
          {account.renews_at && <SummaryRow label="Renewal">{capacityDate(account.renews_at)}</SummaryRow>}
        </dl>
        {account.scheduled_change && <div className="billing-capacity-scheduled">
          <p>Scheduled for {capacityDate(account.scheduled_change.effective_at)}:{' '}
            {capacityMoney(account.scheduled_change.monthly_price_cents)}/month capacity
            {account.scheduled_change.interval === 'year' ? ', billed annually' : ''}.</p>
          {account.actions?.cancel_scheduled && <>
            <label className="billing-capacity-check"><input type="checkbox" checked={cancelConsent}
              onChange={(event) => setCancelConsent(event.target.checked)} disabled={pending || changing} />
              Keep my current recurring amount instead of this scheduled change.</label>
            <button type="button" disabled={!cancelConsent || pending || changing || !account.terms_version || !account.privacy_version}
              onClick={() => billing.submit({ terms_accepted: true, terms_version: account.terms_version,
                privacy_version: account.privacy_version }, '/scheduled/cancel')}>Undo scheduled change</button>
          </>}
        </div>}
        {account.cancel_at_period_end && <p>Your scheduled cancellation remains in place.</p>}
        {account.billing_review_required && <p>Your managed capacity is under billing review following a payment adjustment.
          {' '}Contact <a href="mailto:support@useviola.com">support@useviola.com</a> for help.</p>}
      </>}

      <button type="button" className="billing-capacity-primary" onClick={() => openSelector()}
        disabled={!available || !(account?.actions?.increase || account?.actions?.checkout) || pending || changing}
        aria-expanded={expanded} aria-controls={`${id}-selector`}>Add more usage</button>
      {account?.actions?.lower && <button type="button" onClick={() => openSelector(true)} disabled={!available || pending || changing}>
        Lower my monthly capacity</button>}
      {!available && !loading && <p>Capacity changes are currently unavailable. You can keep using local models or your own provider key on their existing terms.</p>}

      {expanded && available && <div id={`${id}-selector`} className="billing-capacity-selector">
        <h4>Choose your monthly capacity</h4>
        <div className="billing-capacity-interval" role="group" aria-label="Billing interval">
          {['month', 'year'].map((value) => <button key={value} type="button" aria-pressed={interval === value}
            disabled={pending || changing} onClick={() => changeSelection(input, value, false)}>
            {value === 'year' ? 'Annual' : 'Monthly'}</button>)}
        </div>
        <p className="billing-capacity-price">{selection ? capacityMoney(interval === 'year'
          ? selection.annual_price_cents : selectedCents) : 'Choose a valid amount'}{selection && (interval === 'year' ? '/year' : '/month')}</p>
        {interval === 'year' && selection && <p>{capacityMoney(selectedCents)}/month capacity level. Usage resets monthly.</p>}
        {selection && <p><strong>{selection.capacity_multiplier}× Pro</strong> managed AI and phone usage allowance.
          Workloads and models consume it at different rates.</p>}
        <label htmlFor={`${id}-range`}>Monthly capacity level</label>
        <input id={`${id}-range`} ref={selector} type="range" min="0" max={allowed.length - 1} step="1"
          value={selectedIndex < 0 ? 0 : selectedIndex} disabled={pending || changing}
          aria-valuetext={selection ? `${capacityMoney(selectedCents)} monthly capacity, ${selection.capacity_multiplier} times Pro` : 'Choose a valid amount'}
          onChange={(event) => changeSelection(String(allowed[Number(event.target.value)] / 100))} />
        <div className="billing-capacity-input">
          <button type="button" aria-label="Decrease monthly capacity" disabled={pending || changing || selectedIndex <= 0}
            onClick={() => changeSelection(String(allowed[selectedIndex - 1] / 100))}>−</button>
          <label htmlFor={`${id}-amount`}>Monthly dollars
            <input id={`${id}-amount`} inputMode="numeric" type="text" value={input} disabled={pending || changing}
              aria-invalid={selectedIndex < 0} aria-describedby={selectedIndex < 0 ? `${id}-invalid` : undefined}
              onChange={(event) => changeSelection(event.target.value)} />
          </label>
          <button type="button" aria-label="Increase monthly capacity" disabled={pending || changing || selectedIndex < 0 || selectedIndex === allowed.length - 1}
            onClick={() => changeSelection(String(allowed[selectedIndex + 1] / 100))}>+</button>
        </div>
        {selectedIndex < 0 && <p id={`${id}-invalid`} role="alert">Choose $12, $20, or $30–$1,000 in $10 steps.</p>}
        <div className="billing-capacity-presets" role="group" aria-label="Capacity presets">
          {[1200, 2000, 5000, 10000, 20000].filter((amount) => allowed.includes(amount)).map((amount) => (
            <button key={amount} type="button" aria-pressed={selectedCents === amount} disabled={pending || changing}
              onClick={() => changeSelection(String(amount / 100))}>{amount === 1200 ? 'Pro ' : amount === 2000 ? 'Max ' : ''}{capacityMoney(amount)}</button>
          ))}
        </div>
        {selectedIndex === allowed.length - 1 && <p>This is the self-service monthly capacity ceiling. Local models and your own provider key remain available.</p>}
        {selectedCents > account?.monthly_price_cents && account?.monthly_price_cents > 0 && interval === account.interval && <label className="billing-capacity-check">
          <input type="checkbox" checked={temporary} disabled={pending || changing}
            onChange={(event) => changeSelection(input, interval, event.target.checked)} />
          Return to {capacityMoney(account.monthly_price_cents)}/month capacity at {account.interval === 'year' ? 'my annual renewal' : 'renewal'}.
        </label>}
        <p>Opening Billing, changing this selection, and previewing a quote do not charge you. Reductions and interval changes take effect at renewal.</p>
        <button type="button" disabled={selectedIndex < 0 || quoting || pending || changing}
          onClick={() => billing.preview({ monthly_price_cents: selectedCents, interval, return_at_renewal: temporary })}>
          {quoting ? 'Preparing quote…' : 'Preview change'}</button>

        {quote && !validQuote && <p role="alert">The quote is incomplete. Please request a fresh quote.</p>}
        {validQuote && <div className="billing-capacity-quote" aria-label="Change quote">
          <h4>Review your change</h4>
          <dl className="billing-capacity-summary">
            <SummaryRow label="Selected plan">{capacityMoney(quote.target_monthly_price_cents, quote.currency)}/month capacity · {quote.capacity_multiplier}× Pro</SummaryRow>
            <SummaryRow label="Charge today">{capacityMoney(quote.amount_due_now_cents, quote.currency)}{quote.tax_included_in_quote ? ' (tax included)' : ' + applicable tax'}</SummaryRow>
            {Array.isArray(quote.line_items) && quote.line_items.map((line, index) => <SummaryRow key={index} label={line.description}>{capacityMoney(line.amount_cents, quote.currency)}</SummaryRow>)}
            <SummaryRow label="Additional usage this cycle">+{quote.additional_current_cycle_multiplier}× Pro’s full monthly allowance</SummaryRow>
            <SummaryRow label={quote.action === 'schedule' ? 'Full-cycle capacity after change' : 'Next full-cycle allowance'}>
              {quote.next_capacity_multiplier ?? quote.capacity_multiplier}× Pro</SummaryRow>
            {quote.next_recurring_capacity_multiplier !== undefined
              && quote.next_recurring_capacity_multiplier !== (quote.next_capacity_multiplier ?? quote.capacity_multiplier)
              && <SummaryRow label="Capacity at renewal">{quote.next_recurring_capacity_multiplier}× Pro</SummaryRow>}
            <SummaryRow label="Next renewal">{capacityMoney(quote.next_recurring_amount_cents, quote.currency)}{quote.tax_included_in_quote ? '' : ' + applicable tax'}, on {capacityDate(quote.renews_at)}</SummaryRow>
            <SummaryRow label="Effective date">{capacityDate(quote.effective_at)}</SummaryRow>
          </dl>
          <p>{interval === 'year' ? 'Annual increases are charged for the remaining prepaid annual term. ' : ''}
            Your subscription automatically renews until you cancel. Manage payment methods, invoices, and cancellation separately below.
            {' '}See the <a href="https://useviola.com/terms" target="_blank" rel="noopener noreferrer">Terms of Service</a> for the refund policy.</p>
          <label className="billing-capacity-check"><input type="checkbox" checked={consent} disabled={pending || changing}
            onChange={(event) => setConsent(event.target.checked)} />
            <span>I agree to the <a href="https://useviola.com/terms" target="_blank" rel="noopener noreferrer">Terms of Service</a> and{' '}
              <a href="https://useviola.com/privacy" target="_blank" rel="noopener noreferrer">Privacy Policy</a>, and consent to the charge and renewal shown above.</span></label>
          {!quoteUsable && <p role="alert">This quote has expired. Preview again and review the new charge.</p>}
          <button type="button" className="billing-capacity-primary" disabled={!consent || !quoteUsable || pending || changing || quote.action === 'noop'}
            onClick={() => { if (quoteExpiry > Date.now()) billing.submit({ quote_id: quote.quote_id, ...legalRequest }); }}>{changing ? 'Confirming…' : confirmLabel}</button>
        </div>}
      </div>}

      <div role="status" aria-live="polite" aria-atomic="true">
        {['applied', 'succeeded'].includes(status) && <p>Your capacity is updated. Your work is preserved; continue your paused task when you are ready.</p>}
        {status === 'scheduled' && <p>Your change is scheduled for renewal. Your current paid capacity remains available.</p>}
        {status === 'schedule_canceled' && <p>Your scheduled change was undone. Your current recurring amount remains in place.</p>}
        {['failed', 'expired', 'canceled'].includes(status) && <p>The change was {status}. Your current capacity is unchanged. You can preview a new change.</p>}
        {status === 'remediation_required' && <p>This payment needs billing review before the promised capacity can be confirmed.
          {' '}Change reference: {operation.operation_id || operation.id}. Contact{' '}
          <a href="mailto:support@useviola.com">support@useviola.com</a>.</p>}
        {pending && status !== 'remediation_required' && <p>{status === 'requires_action' ? 'Complete payment authentication to apply this change.' : 'Your change is awaiting confirmation. Your current capacity remains available.'}</p>}
      </div>
      {pending && <>
        {operationUrl && <a className="billing-capacity-payment" href={operationUrl} target="_blank" rel="noopener noreferrer">Continue payment</a>}
        <button type="button" disabled={changing} onClick={billing.retry}>{changing ? 'Checking…' : 'Recover change status'}</button>
      </>}
      {error && <p role="alert" className="billing-capacity-error">{error}</p>}
      {!pending && error && <button type="button" onClick={billing.load} disabled={loading}>Retry billing</button>}
    </section>
  );
}

BillingCapacityPanel.propTypes = {
  accountId: PropTypes.string.isRequired,
  refreshUser: PropTypes.func,
  fallback: PropTypes.node,
};
