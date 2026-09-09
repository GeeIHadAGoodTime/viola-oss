#!/bin/sh
set -eu

template="${SEARXNG_SETTINGS_TEMPLATE:-/etc/searxng/settings.yml.template}"
target="${SEARXNG_SETTINGS_RENDERED:-/etc/searxng/settings.yml}"

: "${SEARXNG_SECRET_KEY:?SEARXNG_SECRET_KEY must be set in .env.cloud}"

if [ ! -f "$template" ]; then
    echo "SearXNG settings template not found: $template" >&2
    exit 127
fi

export VIOLA_SEARXNG_SETTINGS_TEMPLATE="$template"
export VIOLA_SEARXNG_SETTINGS_RENDERED="$target"
export SEARXNG_SECRET="${SEARXNG_SECRET:-$SEARXNG_SECRET_KEY}"

/usr/local/searxng/.venv/bin/python - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

template = Path(os.environ["VIOLA_SEARXNG_SETTINGS_TEMPLATE"])
target = Path(os.environ["VIOLA_SEARXNG_SETTINGS_RENDERED"])
secret = os.environ["SEARXNG_SECRET_KEY"]
marker = "${SEARXNG_SECRET_KEY}"

if not secret.strip():
    raise SystemExit("SEARXNG_SECRET_KEY must not be blank")

text = template.read_text(encoding="utf-8")
if marker not in text:
    raise SystemExit("SearXNG settings template is missing ${SEARXNG_SECRET_KEY}")

rendered = text.replace(marker, secret)
for forbidden in ("CHANGE_ME_SEARXNG_KEY", "ultrasecretkey", "change-me"):
    if forbidden.lower() in rendered.lower():
        raise SystemExit("SearXNG rendered settings still contain a placeholder secret")

target.write_text(rendered, encoding="utf-8")
target.chmod(0o600)
PY

unset SEARXNG_SECRET_KEY
exec /usr/local/searxng/entrypoint.sh
