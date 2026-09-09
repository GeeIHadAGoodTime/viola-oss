import { describe, expect, it } from 'vitest';
import { beforeSend } from './sentryEventFilter';

const USER_ID = 'user-normal-123';

const SENSITIVE_KEYS = [
  'access_token',
  'api_key',
  'authorization',
  'byok_api_key',
  'card_number',
  'client_secret',
  'command_text',
  'cookie',
  'credentials',
  'file_contents',
  'oauth_token',
  'openai_api_key',
  'password',
  'payment_cvc',
  'payment_vault',
  'private_key',
  'prompt',
  'refresh_token',
  'screenshot',
  'secret',
  'sentry_dsn',
] as const;

const TIER3_ORIGINS = [
  'agent_audit_log',
  'browser_profile',
  'byok',
  'device_settings',
  'local_calendar_sqlite',
  'oauth_tokens',
  'payment_vault',
  'task_trace',
  'wake_word_model',
] as const;

const SENSITIVE_VALUE_SHAPES = [
  'raw-sensitive-value',
  424242,
  true,
  null,
  { nested: 'raw-sensitive-value', items: [1, 'second-secret'] },
  ['raw-sensitive-value', { deeper: 'second-secret' }],
] as const;

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

const cloneJson = <T extends JsonValue | Record<string, unknown>>(value: T): T =>
  JSON.parse(JSON.stringify(value)) as T;

const sortJson = (value: unknown): unknown => {
  if (Array.isArray(value)) {
    return value.map(sortJson);
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, inner]) => [key, sortJson(inner)])
    );
  }
  return value;
};

const canonical = (value: unknown): string => {
  if (value === undefined) {
    return '__undefined__';
  }
  return JSON.stringify(sortJson(value));
};

const primitiveLeaves = (value: unknown): string[] => {
  if (typeof value === 'string') {
    return [value];
  }
  if (Array.isArray(value)) {
    return value.flatMap(primitiveLeaves);
  }
  if (value && typeof value === 'object') {
    return Object.values(value).flatMap(primitiveLeaves);
  }
  return [];
};

const valueAtPath = (value: unknown, path: string[]): unknown => {
  let current = value;
  for (const part of path) {
    if (!current || typeof current !== 'object' || !(part in current)) {
      return Symbol.for('missing');
    }
    current = (current as Record<string, unknown>)[part];
  }
  return current;
};

const payloadWithProbe = (probe: string): Record<string, unknown> => ({
  event_id: 'evt-pii-probe',
  message: `probe value: ${probe}`,
  exception: { values: [{ type: 'RuntimeError', value: `failure for ${probe}` }] },
  breadcrumbs: { values: [{ message: `breadcrumb ${probe}` }] },
  contexts: { viola: { contact: probe, surface: 'desktop' } },
  extra: { nested: [{ note: probe }], user_id: USER_ID },
  user: { id: USER_ID },
});

const generatedPiiProbes = (): string[] => {
  const probes: string[] = [];
  for (let index = 0; index < 50; index += 1) {
    const suffix = String(index).padStart(4, '0');
    probes.push(`user${suffix}@example${index}.test`);
    probes.push(`312-555-${suffix}`);
    probes.push(`123-45-${suffix}`);
    probes.push('4111 1111 1111 1111');
  }
  return probes;
};

const makeRng = (seed: number): (() => number) => {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0x100000000;
  };
};

const randomInt = (rng: () => number, max: number): number => Math.floor(rng() * max);

const randomToken = (rng: () => number, length = 24): string => {
  const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  return Array.from({ length }, () => alphabet[randomInt(rng, alphabet.length)]).join('');
};

const probeBundle = (rng: () => number, index: number): Record<string, string> => ({
  email: `probe-${String(index).padStart(4, '0')}-${randomToken(rng, 8).toLowerCase()}@example.com`,
  phone: `312-555-${String(index).padStart(4, '0')}`,
  sk_token: `sk-${randomToken(rng, 32)}`,
  card: '4111 1111 1111 1111',
  ssn: `${100 + index}-45-${String(index).padStart(4, '0')}`,
  authorization: `Bearer ${randomToken(rng, 32)}`,
  viola_header_value: `viola-key-${randomToken(rng, 24)}`,
});

const payloadWithProbeBundle = (probes: Record<string, string>): Record<string, unknown> => ({
  event_id: 'evt-pii-bundle',
  message: [probes.email, probes.phone, probes.sk_token, probes.card, probes.ssn].join(' '),
  exception: {
    values: [{ type: 'RuntimeError', value: `failed for ${probes.email} with ${probes.sk_token}` }],
  },
  breadcrumbs: {
    values: [
      { message: `breadcrumb ${probes.phone} ${probes.card}` },
      { data: { url: `/v1/status?token=${probes.sk_token}` } },
    ],
  },
  contexts: { viola: { contact: probes.email, auth: probes.authorization } },
  extra: { nested: [{ note: probes.ssn, token_text: probes.sk_token }], user_id: USER_ID },
  request: {
    url: '/v1/ordinary',
    headers: {
      Authorization: probes.authorization,
      Cookie: `session=${probes.viola_header_value}`,
      'X-Viola-API-Key': probes.viola_header_value,
      'Content-Type': 'application/json',
    },
    cookies: { session: probes.viola_header_value },
    data: { message: probes.email },
  },
  user: { id: USER_ID },
});

const randomScalar = (rng: () => number): JsonValue => {
  const choice = randomInt(rng, 5);
  if (choice === 0) return null;
  if (choice === 1) return randomInt(rng, 2) === 1;
  if (choice === 2) return randomInt(rng, 200000) - 100000;
  if (choice === 3) return rng();
  return `value-${randomInt(rng, 0xffffffff).toString(16).padStart(8, '0')}`;
};

const randomJson = (rng: () => number, depth = 0): JsonValue => {
  if (depth >= 4) {
    return randomScalar(rng);
  }

  const choice = randomInt(rng, 4);
  if (choice === 0) {
    return randomScalar(rng);
  }
  if (choice === 1) {
    return Array.from({ length: randomInt(rng, 4) }, () => randomJson(rng, depth + 1));
  }

  const keys = [
    'message',
    'metadata',
    'safe',
    'user_id',
    'api_key',
    'origin',
    'storage_tier',
    'context',
    'token',
  ];
  return Object.fromEntries(
    Array.from({ length: randomInt(rng, 5) }, (_unused, index) => [
      `${keys[randomInt(rng, keys.length)]}_${index}`,
      randomJson(rng, depth + 1),
    ])
  );
};

const randomEvent = (rng: () => number, index: number): Record<string, unknown> => ({
  event_id: `evt-random-${String(index).padStart(4, '0')}`,
  message: `random payload ${String(index).padStart(4, '0')}`,
  tags: randomJson(rng),
  contexts: { generated: randomJson(rng) },
  extra: randomJson(rng),
  user: { id: USER_ID },
});

describe('Sentry PII beforeSend oracle', () => {
  it('removes generated PII probes or drops the event', () => {
    for (const probe of generatedPiiProbes()) {
      const filtered = beforeSend(cloneJson(payloadWithProbe(probe)), {});
      if (filtered === null) {
        continue;
      }
      expect(canonical(filtered)).not.toContain(probe);
    }
  });

  it('scrubs 100 injected probe bundles across message, extra, breadcrumbs, and request', () => {
    const rng = makeRng(20260607);
    for (let index = 0; index < 100; index += 1) {
      const probes = probeBundle(rng, index);
      const filtered = beforeSend(cloneJson(payloadWithProbeBundle(probes)), {});

      expect(filtered).not.toBeNull();
      const output = canonical(filtered);
      for (const probe of Object.values(probes)) {
        expect(output).not.toContain(probe);
      }
      expect((filtered as { user: { id: string } }).user.id).toBe(USER_ID);
      expect((filtered as { extra: { user_id: string } }).extra.user_id).toBe(USER_ID);
      expect((filtered as { request: { headers: Record<string, string> } }).request.headers).toEqual({
        'Content-Type': 'application/json',
      });
      expect((filtered as { request: Record<string, unknown> }).request).not.toHaveProperty('cookies');
    }
  });

  it.each(TIER3_ORIGINS)('drops Tier-3 origin %s', (origin) => {
    const event = {
      event_id: `evt-tier3-${origin}`,
      message: 'local-only origin attempted to report',
      tags: { origin },
      contexts: { viola: { origin } },
      extra: { source: origin, user_id: USER_ID },
      user: { id: USER_ID },
    };

    expect(beforeSend(cloneJson(event), {})).toBeNull();
  });

  it.each(SENSITIVE_KEYS)('scrubs sensitive key %s for every value shape', (key) => {
    for (const value of SENSITIVE_VALUE_SHAPES) {
      const event = {
        event_id: 'evt-sensitive-key',
        message: 'shape scrub check',
        extra: { safe: 'kept', [key]: cloneJson(value as JsonValue), user_id: USER_ID },
        user: { id: USER_ID },
      };

      const filtered = beforeSend(cloneJson(event), {});

      expect(filtered).not.toBeNull();
      expect((filtered as { extra: { safe: string } }).extra.safe).toBe('kept');
      expect(valueAtPath(filtered, ['extra', key])).not.toEqual(value);
      for (const leaf of primitiveLeaves(value)) {
        expect(canonical(filtered)).not.toContain(leaf);
      }
    }
  });

  it('keeps a normal user_id', () => {
    const event = {
      event_id: 'evt-user-id',
      message: 'non-sensitive desktop startup error',
      tags: { feature: 'settings', user_id: USER_ID },
      extra: { user_id: USER_ID, status: 'retryable' },
      user: { id: USER_ID },
    };

    const filtered = beforeSend(cloneJson(event), {});

    expect(filtered).not.toBeNull();
    expect((filtered as { user: { id: string } }).user.id).toBe(USER_ID);
    expect((filtered as { extra: { user_id: string } }).extra.user_id).toBe(USER_ID);
    expect((filtered as { tags: { user_id: string } }).tags.user_id).toBe(USER_ID);
  });

  it('does not crash on 1000 random JSON payloads and is deterministic', () => {
    const rng = makeRng(61107);
    for (let index = 0; index < 1000; index += 1) {
      const event = randomEvent(rng, index);

      const first = beforeSend(cloneJson(event), {});
      const second = beforeSend(cloneJson(event), {});

      expect(canonical(first), `non-deterministic output at payload ${index}`).toBe(canonical(second));
    }
  });
});
