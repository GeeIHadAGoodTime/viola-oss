"""
Secrets Masking Utility.

Provides functions to mask sensitive data in logs and error messages.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SecretMatch:
    """A high-confidence secret pattern match without the matched value."""

    rule_id: str
    label: str


@dataclass(frozen=True)
class _SecretRule:
    rule_id: str
    label: str
    pattern: re.Pattern[str]


_BOUNDARY = r"(?:[`'\"\s;]|\\[nr]|$)"
_SECRET_RULES: tuple[_SecretRule, ...] = (
    _SecretRule(
        "aws-access-token", "AWS_ACCESS_TOKEN", re.compile(r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b")
    ),
    _SecretRule("gcp-api-key", "GCP_API_KEY", re.compile(r"\b(AIza[\w-]{35})%s" % _BOUNDARY)),
    _SecretRule(
        "azure-ad-client-secret",
        "AZURE_AD_CLIENT_SECRET",
        re.compile(r"(?:^|[\\'\"`\s>=:(,)])([a-zA-Z0-9_~.]{3}\dQ~[a-zA-Z0-9_~.-]{31,34})(?:$|[\\'\"`\s<),])"),
    ),
    _SecretRule("digitalocean-pat", "DIGITALOCEAN_PAT", re.compile(r"\b(dop_v1_[a-f0-9]{64})%s" % _BOUNDARY)),
    _SecretRule(
        "digitalocean-access-token", "DIGITALOCEAN_ACCESS_TOKEN", re.compile(r"\b(doo_v1_[a-f0-9]{64})%s" % _BOUNDARY)
    ),
    _SecretRule(
        "anthropic-api-key",
        "ANTHROPIC_API_KEY",
        re.compile(r"\b(sk-ant(?:-api)?03-[a-zA-Z0-9_-]{20,}AA)%s" % _BOUNDARY),
    ),
    _SecretRule(
        "anthropic-admin-api-key",
        "ANTHROPIC_ADMIN_API_KEY",
        re.compile(r"\b(sk-ant-admin01-[a-zA-Z0-9_-]{20,}AA)%s" % _BOUNDARY),
    ),
    _SecretRule(
        "openai-api-key",
        "OPENAI_API_KEY",
        re.compile(
            r"\b(sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}|sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})%s" % _BOUNDARY
        ),
    ),
    _SecretRule(
        "jwt",
        "JWT",
        re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"),
    ),
    _SecretRule(
        "huggingface-access-token", "HUGGINGFACE_ACCESS_TOKEN", re.compile(r"\b(hf_[a-zA-Z]{34})%s" % _BOUNDARY)
    ),
    _SecretRule("github-pat", "GITHUB_PAT", re.compile(r"\b(ghp_[0-9a-zA-Z]{36})\b")),
    _SecretRule("github-fine-grained-pat", "GITHUB_FINE_GRAINED_PAT", re.compile(r"\b(github_pat_\w{82})\b")),
    _SecretRule("github-app-token", "GITHUB_APP_TOKEN", re.compile(r"\b((?:ghu|ghs)_[0-9a-zA-Z]{36})\b")),
    _SecretRule("github-oauth", "GITHUB_OAUTH", re.compile(r"\b(gho_[0-9a-zA-Z]{36})\b")),
    _SecretRule("github-refresh-token", "GITHUB_REFRESH_TOKEN", re.compile(r"\b(ghr_[0-9a-zA-Z]{36})\b")),
    _SecretRule("gitlab-pat", "GITLAB_PAT", re.compile(r"\b(glpat-[\w-]{20})\b")),
    _SecretRule("gitlab-deploy-token", "GITLAB_DEPLOY_TOKEN", re.compile(r"\b(gldt-[0-9a-zA-Z_-]{20})\b")),
    _SecretRule("slack-bot-token", "SLACK_BOT_TOKEN", re.compile(r"\b(xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*)\b")),
    _SecretRule(
        "slack-user-token", "SLACK_USER_TOKEN", re.compile(r"\b(xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34})\b")
    ),
    _SecretRule(
        "slack-app-token", "SLACK_APP_TOKEN", re.compile(r"\b(xapp-\d-[A-Z0-9]+-\d+-[a-z0-9]+)\b", re.IGNORECASE)
    ),
    _SecretRule("twilio-api-key", "TWILIO_API_KEY", re.compile(r"\b(SK[0-9a-fA-F]{32})\b")),
    _SecretRule("sendgrid-api-token", "SENDGRID_API_TOKEN", re.compile(r"\b(SG\.[a-zA-Z0-9=_\-.]{66})%s" % _BOUNDARY)),
    _SecretRule("npm-access-token", "NPM_ACCESS_TOKEN", re.compile(r"\b(npm_[a-zA-Z0-9]{36})%s" % _BOUNDARY)),
    _SecretRule("pypi-upload-token", "PYPI_UPLOAD_TOKEN", re.compile(r"\b(pypi-AgEIcHlwaS5vcmc[\w-]{50,1000})\b")),
    _SecretRule(
        "databricks-api-token", "DATABRICKS_API_TOKEN", re.compile(r"\b(dapi[a-f0-9]{32}(?:-\d)?)%s" % _BOUNDARY)
    ),
    _SecretRule("pulumi-api-token", "PULUMI_API_TOKEN", re.compile(r"\b(pul-[a-f0-9]{40})%s" % _BOUNDARY)),
    _SecretRule("postman-api-token", "POSTMAN_API_TOKEN", re.compile(r"\b(PMAK-[a-fA-F0-9]{24}-[a-fA-F0-9]{34})\b")),
    _SecretRule(
        "grafana-cloud-api-token",
        "GRAFANA_CLOUD_API_TOKEN",
        re.compile(r"\b(glc_[A-Za-z0-9+/]{32,400}={0,3})%s" % _BOUNDARY),
    ),
    _SecretRule(
        "grafana-service-account-token",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
        re.compile(r"\b(glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8})%s" % _BOUNDARY),
    ),
    _SecretRule("sentry-user-token", "SENTRY_USER_TOKEN", re.compile(r"\b(sntryu_[a-f0-9]{64})%s" % _BOUNDARY)),
    _SecretRule(
        "stripe-access-token",
        "STRIPE_ACCESS_TOKEN",
        re.compile(r"\b((?:sk|rk)_(?:test|live|prod)_[a-zA-Z0-9]{10,99})%s" % _BOUNDARY),
    ),
    _SecretRule("shopify-access-token", "SHOPIFY_ACCESS_TOKEN", re.compile(r"\b(shpat_[a-fA-F0-9]{32})\b")),
    _SecretRule("shopify-shared-secret", "SHOPIFY_SHARED_SECRET", re.compile(r"\b(shpss_[a-fA-F0-9]{32})\b")),
    _SecretRule(
        "private-key",
        "PRIVATE_KEY",
        re.compile(
            r"(-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S-]{64,}?-----END[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----)"
        ),
    ),
)

_DEFAULT_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "auth_key",
    "access_token",
    "refresh_token",
    "credential",
    "authorization",
    "basic_auth",
    "cookie",
    "client_secret",
    "id_token",
    "jwt",
    "private_key",
    "sentry_dsn",
    "confirmation_token",
    "payment_confirmation_token",
    "link_token",
    "body",
    "command_text",
    "prompt",
    "file_contents",
    "screenshot",
    "payment_details",
}

_CONTENT_KEY_MARKERS = {
    "body",
    "command_text",
    "prompt",
    "file_contents",
    "screenshot",
    "payment_details",
}

_REDACTION_PLACEHOLDERS = {
    "[FILTERED]",
    "[REDACTED]",
    "***REDACTED***",
    "****REDACTED****",
}


def mask_secret(value: str, visible_chars: int = 4) -> str:
    """
    Mask a secret, showing only first and last N chars.

    Args:
        value: The secret to mask
        visible_chars: Number of chars to show at start and end

    Returns:
        Masked string like "z9aJ****vgA"
    """
    if not value:
        return "****"
    if len(value) <= visible_chars * 2:
        return "*" * len(value)
    return value[:visible_chars] + "*" * (len(value) - visible_chars * 2) + value[-visible_chars:]


def scan_secrets_in_text(text: str) -> list[SecretMatch]:
    """Return high-confidence secret rule labels without exposing values."""

    if not text:
        return []
    matches: list[SecretMatch] = []
    seen: set[str] = set()
    for rule in _SECRET_RULES:
        if rule.rule_id in seen:
            continue
        if rule.pattern.search(text):
            seen.add(rule.rule_id)
            matches.append(SecretMatch(rule_id=rule.rule_id, label=rule.label))
    return matches


def _redact_rule_match(match: re.Match[str], label: str) -> str:
    placeholder = "[REDACTED:%s]" % label
    if match.lastindex:
        captured = match.group(1)
        if isinstance(captured, str):
            return match.group(0).replace(captured, placeholder, 1)
    return placeholder


def _redact_rule(rule: _SecretRule, text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return _redact_rule_match(match, rule.label)

    return rule.pattern.sub(_replace, text)


def redact_secret_spans_in_text(text: str) -> str:
    """Redact only curated high-confidence secret spans."""

    if not text:
        return text

    result = text
    for rule in _SECRET_RULES:
        result = _redact_rule(rule, result)
    return result


def mask_secrets_in_text(text: str, *, include_card_data: bool = True) -> str:
    """
    Mask detected secrets in a text string using regex patterns.

    Detects and masks:
    - API keys (api_key=..., api-key:...)
    - Tokens (token=..., bearer ...)
    - Passwords (password=...)
    - OpenAI keys (sk-...)
    """
    if not text:
        return text
    if include_card_data:
        try:
            from intent.log_redaction import redact_card_data

            text = redact_card_data(text)
        except Exception:
            logging.getLogger(__name__).debug("Card-data redaction failed for text payload")

    query_param_patterns = [
        (
            r"([?&](?:key|api_key|apikey|access_token|refresh_token|id_token|client_secret|token|jwt|password|secret|authorization|session|sessionid|sid|sig|signature|code)=)([^&\s\"']+)",
            r"\g<1>****REDACTED****",
        )
    ]
    generic_patterns = [
        (r"(^|[^A-Za-z0-9_])((?:api[_-]?key)[=:\t \"']+)([a-zA-Z0-9_-]{16,})", r"\g<1>\g<2>****REDACTED****"),
        (r"(^|[^A-Za-z0-9_])((?:token)[=:\t \"']+)([a-zA-Z0-9_.-]{16,})", r"\g<1>\g<2>****REDACTED****"),
        (r"(^|[^A-Za-z0-9_])((?:secret)[=:\t \"']+)([a-zA-Z0-9_-]{16,})", r"\g<1>\g<2>****REDACTED****"),
        (r"(^|[^A-Za-z0-9_])((?:password)[=:\t \"']+)([^\s&\"']+)", r"\g<1>\g<2>****REDACTED****"),
        (r"(^|[^A-Za-z0-9_])((?:bearer)[ \t]+)([a-zA-Z0-9_.-]+)", r"\g<1>\g<2>****REDACTED****"),
        (r"(^|[^A-Za-z0-9_])((?:basic)[ \t]+)([A-Za-z0-9+/=]{12,})", r"\g<1>\g<2>****REDACTED****"),
        (r"(AIza[0-9A-Za-z_-]{20,})", r"AIza****REDACTED****"),
        (r"(GOCSPX-[0-9A-Za-z_-]{20,})", r"GOCSPX-****REDACTED****"),
        (r"(sk-ant-[0-9A-Za-z_-]{20,})", r"sk-ant-****REDACTED****"),
        (r"(sk-[a-zA-Z0-9_-]{20,})", r"sk-****REDACTED****"),
    ]

    result = text
    for pattern, replacement in query_param_patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    result = redact_secret_spans_in_text(result)
    for pattern, replacement in generic_patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE | re.MULTILINE)
    return result


def contains_maskable_secret(text: str) -> bool:
    """Return True when the shared masking rules identify secret material."""
    if not text:
        return False
    return mask_secrets_in_text(text) != text


def mask_dict_secrets(
    data: dict[str, Any],
    sensitive_keys: set[str] | None = None,
) -> dict[str, Any]:
    """
    Recursively mask secrets in a dictionary.

    Args:
        data: Dictionary to mask
        sensitive_keys: Set of key names to mask (case-insensitive partial match)

    Returns:
        New dictionary with secrets masked
    """
    if sensitive_keys is None:
        sensitive_keys = set(_DEFAULT_SENSITIVE_KEYS)

    try:
        from intent.log_redaction import redact_card_data

        data = redact_card_data(data)
    except Exception:
        logging.getLogger(__name__).debug("Card-data redaction failed for mapping payload")

    result: dict[str, Any] = {}
    for key, value in data.items():
        key_lower = key.lower()
        key_is_sensitive = any(sk in key_lower for sk in sensitive_keys)
        if key_is_sensitive:
            result[key] = _mask_sensitive_mapping_value(key_lower, value)
        elif isinstance(value, dict):
            result[key] = mask_dict_secrets(value, sensitive_keys)
        elif isinstance(value, list):
            result[key] = [
                (
                    mask_dict_secrets(item, sensitive_keys)
                    if isinstance(item, dict)
                    else mask_secrets_in_text(item) if isinstance(item, str) else item
                )
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = mask_secrets_in_text(value)
        else:
            result[key] = value
    return result


def _mask_sensitive_mapping_value(key_lower: str, value: Any) -> Any:
    if value is None:
        return None
    if any(marker in key_lower for marker in _CONTENT_KEY_MARKERS):
        return "[REDACTED]"
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in _REDACTION_PLACEHOLDERS or normalized.startswith("[REDACTED:"):
            return value
        return mask_secret(value)
    return "[REDACTED]"
