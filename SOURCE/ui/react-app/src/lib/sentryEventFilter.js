const FILTERED_VALUE = '[Filtered]';
const SKIP_FIELD = Symbol('skip-field');

export const SENTRY_SCRUB_FIELDS = Object.freeze([
  'password',
  'secret',
  'api_key',
  'private_key',
  'credentials',
  'body',
  'request_body',
  'email_body',
  'message_body',
  'command_text',
  'prompt',
  'file_contents',
  'screenshot',
  'payment_details',
  'payment_method',
  'payment_vault',
  'confirmation_token',
  'payment_confirmation_token',
  'token',
  'access_token',
  'refresh_token',
  'oauth_token',
  'authorization',
  'bearer',
  'jwt',
  'session',
  'cookie',
  'webhook',
  'openai_api_key',
  'anthropic_api_key',
  'google_api_key',
  'stripe_key',
  'publishable_key',
  'btcpay_api_key',
  'resend_api_key',
  'sentry_dsn',
  'youtube_api_key',
  'client_id',
  'client_secret',
  'team_id',
  'key_id',
  'card_number',
  'cardnumber',
  'card_cvc',
  'card_cvv',
  'card_security_code',
  'cvc',
  'cvv',
  'local_payment_cvc',
  'payment_cvc',
  'primary_account_number',
  'security_code',
]);

export const SENSITIVE_HEADER_NAMES = Object.freeze([
  'authorization',
  'proxy-authorization',
  'cookie',
  'set-cookie',
  'x-api-key',
  'x-viola-api-key',
  'x-auth-token',
  'x-access-token',
  'x-refresh-token',
  'x-session-token',
  'x-csrf-token',
  'x-xsrf-token',
  'csrf-token',
  'apikey',
  'sec-websocket-protocol',
  'x-forwarded-for',
  'x-real-ip',
]);

const USER_ID_KEYS = Object.freeze(['id', 'user_id', 'userId']);
const USER_PII_KEYS = Object.freeze(['email', 'username', 'ip_address', 'ipAddress', 'name']);
const COOKIE_KEYS = Object.freeze(['cookie', 'cookies']);
const HEADER_KEYS = Object.freeze(['headers', 'request_headers', 'response_headers']);
const REQUEST_BODY_KEYS = Object.freeze(['body', 'data']);

const TIER3_TIER_VALUE_RE = /\b(?:tier[-_\s]?3|local[-_\s]?only|desktop[-_\s]?only|never[-_\s]?cloud)\b/i;
const TIER3_ORIGIN_VALUE_RE = /\b(?:payment[-_\s]?(?:vault|card|method)|byok|bring[-_\s]?your[-_\s]?own[-_\s]?key|api[-_\s]?vault|credential[-_\s]?vault|oauth[-_\s]?(?:token|tokens|credentials|token[-_\s]?vault)|token[-_\s]?vault|browser[-_\s]?(?:profile|profiles|artifacts|voice[-_\s]?usage)|cookie[-_\s]?(?:jar|export)|agent[-_\s]?audit|audit[-_\s]?log|device[-_\s]?settings|wake[-_\s]?word|custom[-_\s]?wake[-_\s]?word[-_\s]?model|local[-_\s]?calendar|calendar\.sqlite|events\.db|keyring|secure[-_\s]?settings|vault[-_\s]?master[-_\s]?key|os[-_\s]?keyring|trace[-_\s]?(?:store|file|files|db|database)|task[-_\s]?(?:trace|checkpoint)|state[-_\s]?store[-_\s]?snapshot|state\.sqlite3|command[-_\s]?ledger|phone[-_\s]?call[-_\s]?history|codex[-_\s]?auth|gotrue[-_\s]?tokens|gemini[-_\s]?cli[-_\s]?workspace[-_\s]?token|\/(?:traces|browser_profiles|audit_logs|payment_vault|credential_vault|api_vault|tasks|call_history|browser_voice_usage)(?:\/|$))\b/i;
const TIER_KEY_RE = /(?:^|[_-])(?:storage|data|privacy)?[_-]?tier$/i;
const ORIGIN_FIELD_NAMES = Object.freeze([
  'origin',
  'source',
  'surface',
  'subsystem',
  'component',
  'logger',
  'transaction',
  'route',
  'url',
  'path',
  'filename',
  'abs_path',
  'module',
  'category',
  'message',
]);

const EMAIL_RE = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi;
const SSN_RE = /\b\d{3}-\d{2}-\d{4}\b/g;
const PHONE_RE = /\b(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}\b/g;
const CARD_CANDIDATE_RE = /\b(?:\d[ -]?){13,19}\b/g;
const CVC_RE = /\b((?:cvc|cvv|security code)\s*[:=]?\s*)\d{3,4}\b/gi;
const SECRET_ASSIGNMENT_RE = /\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|jwt|password|secret|client[_-]?secret|authorization|bearer|session|cookie)\b\s*[:=]\s*)([^\s"',;&)]+)/gi;
const URL_SECRET_PARAM_RE = /([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|jwt|token|password|secret|client[_-]?secret|code|state)=)[^&#\s]+/gi;
const AUTH_VALUE_RE = /\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi;
const OPENAI_STYLE_KEY_RE = /\b(?:sk|pk)-(?:live-|test-)?[A-Za-z0-9_-]{16,}\b/g;
const STRIPE_STYLE_KEY_RE = /\b[sp]k_(?:live|test)_[A-Za-z0-9_]{8,}\b/g;
const IPV4_RE = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;
const WINDOWS_USER_PATH_RE = /\b([A-Z]:\\Users\\)[^\\\s]+/gi;

/**
 * Sentry browser SDK beforeSend hook.
 *
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} _hint
 * @returns {Record<string, unknown> | null}
 */
export function beforeSend(event, _hint) {
  void _hint;
  return filterSentryEvent(event);
}

/**
 * Drop Tier-3-origin events and scrub retained events before Sentry upload.
 *
 * @param {unknown} event
 * @returns {Record<string, unknown> | null}
 */
export function filterSentryEvent(event) {
  if (!isObjectLike(event)) {
    return null;
  }

  if (isTier3OriginEvent(event)) {
    return null;
  }

  return scrubSentryEvent(event);
}

/**
 * True when structured event context points at a desktop-only Tier-3 surface.
 *
 * @param {unknown} event
 * @returns {boolean}
 */
export function isTier3OriginEvent(event) {
  return findTier3Origin(event, [], new WeakSet());
}

/**
 * Return a sanitized copy of the event. The input event is not mutated.
 *
 * @param {Record<string, unknown>} event
 * @returns {Record<string, unknown>}
 */
export function scrubSentryEvent(event) {
  const scrubbed = sanitizeValue(event, [], new WeakSet());
  if (!isObjectLike(scrubbed) || Array.isArray(scrubbed)) {
    return {};
  }
  return scrubbed;
}

function findTier3Origin(value, path, seen) {
  if (value === null || value === undefined) {
    return false;
  }

  if (typeof value === 'string') {
    return tier3ValueMatches(path, value);
  }

  if (typeof value !== 'object') {
    return false;
  }

  if (seen.has(value)) {
    return false;
  }
  seen.add(value);

  if (Array.isArray(value)) {
    return value.some((item, index) => findTier3Origin(item, path.concat(String(index)), seen));
  }

  for (const [key, child] of Object.entries(value)) {
    const keyLower = key.toLowerCase();
    const nextPath = path.concat(key);

    if (TIER_KEY_RE.test(keyLower) && typeof child === 'string' && TIER3_TIER_VALUE_RE.test(child)) {
      return true;
    }

    if (findTier3Origin(child, nextPath, seen)) {
      return true;
    }
  }

  return false;
}

function tier3ValueMatches(path, value) {
  const key = String(path[path.length - 1] || '').toLowerCase();
  if (TIER_KEY_RE.test(key)) {
    return TIER3_TIER_VALUE_RE.test(value);
  }
  if (ORIGIN_FIELD_NAMES.includes(key)) {
    return TIER3_ORIGIN_VALUE_RE.test(value) || TIER3_TIER_VALUE_RE.test(value);
  }
  return false;
}

function sanitizeValue(value, path, seen) {
  if (value === null || value === undefined) {
    return value;
  }

  if (typeof value === 'string') {
    return redactText(value);
  }

  if (typeof value !== 'object') {
    return value;
  }

  if (seen.has(value)) {
    return FILTERED_VALUE;
  }
  seen.add(value);

  if (Array.isArray(value)) {
    const sanitized = [];
    value.forEach((item, index) => {
      const result = sanitizeValue(item, path.concat(String(index)), seen);
      if (result !== SKIP_FIELD) {
        sanitized.push(result);
      }
    });
    return sanitized;
  }

  return sanitizeObject(value, path, seen);
}

function sanitizeObject(value, path, seen) {
  const output = {};
  const insideRequest = String(path[path.length - 1] || '').toLowerCase() === 'request';
  const insideUser = String(path[path.length - 1] || '').toLowerCase() === 'user';

  for (const [key, child] of Object.entries(value)) {
    const keyLower = key.toLowerCase();
    const normalizedKey = normalizeHeaderName(key);
    const childPath = path.concat(key);

    if (COOKIE_KEYS.includes(normalizedKey)) {
      continue;
    }

    if (insideRequest && REQUEST_BODY_KEYS.includes(normalizedKey)) {
      continue;
    }

    if (HEADER_KEYS.includes(normalizedKey)) {
      const headers = sanitizeHeaders(child, childPath, seen);
      if (headers !== SKIP_FIELD) {
        output[key] = headers;
      }
      continue;
    }

    if (insideUser && USER_ID_KEYS.includes(key)) {
      output[key] = redactTextIfPii(child);
      continue;
    }

    if (insideUser && USER_PII_KEYS.includes(key)) {
      output[key] = FILTERED_VALUE;
      continue;
    }

    if (isSensitiveFieldKey(keyLower)) {
      output[key] = FILTERED_VALUE;
      continue;
    }

    const result = sanitizeValue(child, childPath, seen);
    if (result !== SKIP_FIELD) {
      output[key] = result;
    }
  }

  return output;
}

function sanitizeHeaders(headers, path, seen) {
  if (headers === null || headers === undefined) {
    return SKIP_FIELD;
  }

  if (Array.isArray(headers)) {
    const sanitized = [];
    headers.forEach((header, index) => {
      if (Array.isArray(header) && header.length > 0) {
        const [name, ...rest] = header;
        if (!isSensitiveHeaderName(name)) {
          sanitized.push([name, ...rest.map((value) => sanitizeValue(value, path.concat(String(index)), seen))]);
        }
        return;
      }
      const result = sanitizeValue(header, path.concat(String(index)), seen);
      if (result !== SKIP_FIELD) {
        sanitized.push(result);
      }
    });
    return sanitized;
  }

  if (!isObjectLike(headers)) {
    return FILTERED_VALUE;
  }

  const output = {};
  for (const [key, value] of Object.entries(headers)) {
    if (isSensitiveHeaderName(key)) {
      continue;
    }
    output[key] = sanitizeValue(value, path.concat(key), seen);
  }
  return output;
}

function isSensitiveFieldKey(keyLower) {
  return SENTRY_SCRUB_FIELDS.some((field) => keyLower.includes(field));
}

function isSensitiveHeaderName(name) {
  const normalized = normalizeHeaderName(name);
  return SENSITIVE_HEADER_NAMES.includes(normalized)
    || normalized.includes('authorization')
    || normalized.includes('cookie')
    || normalized.includes('api-key')
    || normalized.includes('apikey')
    || normalized.includes('token')
    || normalized.includes('secret')
    || normalized.includes('session')
    || normalized.includes('jwt')
    || normalized.includes('csrf');
}

function redactTextIfPii(value) {
  if (typeof value !== 'string') {
    return value;
  }
  return redactText(value);
}

export function redactText(text) {
  if (typeof text !== 'string' || text.length === 0) {
    return text;
  }

  return text
    .replace(WINDOWS_USER_PATH_RE, '$1[REDACTED_USER]')
    .replace(URL_SECRET_PARAM_RE, '$1[Filtered]')
    .replace(SECRET_ASSIGNMENT_RE, '$1[Filtered]')
    .replace(AUTH_VALUE_RE, '[Filtered]')
    .replace(OPENAI_STYLE_KEY_RE, '[Filtered]')
    .replace(STRIPE_STYLE_KEY_RE, '[Filtered]')
    .replace(EMAIL_RE, '[REDACTED_EMAIL]')
    .replace(SSN_RE, '[REDACTED_SSN]')
    .replace(PHONE_RE, '[REDACTED_PHONE]')
    .replace(CVC_RE, '$1[Filtered]')
    .replace(CARD_CANDIDATE_RE, '[REDACTED_CARD]')
    .replace(IPV4_RE, (candidate) => (isIpv4(candidate) ? '[REDACTED:IP]' : candidate));
}

function isIpv4(candidate) {
  return candidate.split('.').every((part) => {
    if (part.length > 1 && part.startsWith('0')) {
      return false;
    }
    const number = Number(part);
    return Number.isInteger(number) && number >= 0 && number <= 255;
  });
}

function normalizeHeaderName(name) {
  return String(name).trim().toLowerCase();
}

function isObjectLike(value) {
  return value !== null && typeof value === 'object';
}

export default beforeSend;
