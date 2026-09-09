"""Build identity of the running Viola process (#4811).

Production could not answer "which commit is live?" over any public surface.
``/health`` returned only status/service/uptime, ``/v1/version`` and
``/api/version`` are desktop-only routes that 404 on the cloud, and the one
place the SHA did exist -- the ``viola_deployed_sha_info`` Prometheus gauge in
``backend/observability_routes.py`` -- is scraped only from INSIDE the container
(``docker exec ... curl localhost:8080/metrics``). So no agent could verify what
was live without box access, and deploy lag kept masquerading as code bugs
(candidate C-689).

Two environment variables carry a SHA into the container, and they are not the
same thing:

* ``VIOLA_BUILD_SHA`` -- a Docker BUILD ARG, baked into the image at build time
  by ``Dockerfile.cloud`` (both the spa-builder and runtime stages). This is the
  tamper-evident one: it is a property of the image itself, so a compose file
  cannot misreport it, and it is the same value the ``/app`` SPA bundle carries
  as ``import.meta.env.VITE_VIOLA_BUILD_SHA``. Until #4811 the prod build path
  (``.github/workflows/prod-image-build.yml``) never passed it, so the bundle
  baked the literal ``dev``.
* ``VIOLA_DEPLOYED_SHA`` -- a RUNTIME env var set at swap time by the deploy
  path (``docker-compose.cloud.yml`` requires it; ``scripts/publish/roll_steps.py``
  and ``scripts/launch/deploy_cloud.ps1`` set it). It already feeds the metrics
  gauge. It is a claim made by whoever started the container, not a property of
  the image -- so it is the fallback, never the first answer.

On a correct roll both hold the same value. Reporting which one answered is the
point of ``build_sha_source()``: ``deploy_env`` on a live deploy means the image
was built without the build arg, i.e. the #4811 regression is back.

Exposure note: the SHA is not a secret. The same value is already baked into the
public ``/app`` SPA bundle served to every browser, so surfacing it on ``/health``
reveals nothing new -- it just makes the identity readable without downloading
and grepping the bundle.
"""

from __future__ import annotations

import os
import re

#: Baked into the image at build time (Docker build arg). Authoritative.
BUILD_SHA_ENV = "VIOLA_BUILD_SHA"
#: Set at container-start time by the deploy path. Fallback only.
DEPLOYED_SHA_ENV = "VIOLA_DEPLOYED_SHA"

#: What ``build_sha()`` reports when the process carries no usable build
#: identity (a local dev run, or an image built without the build arg).
#: Deliberately not an empty string and deliberately not ``"dev"``: a reader
#: must be able to tell "this deploy cannot identify itself" apart from a real
#: SHA at a glance. Matches the ``unknown`` sentinel the deployed-SHA metric
#: already uses, so the two surfaces read the same.
UNKNOWN_BUILD_SHA = "unknown"

#: Which env var answered: the build arg, the deploy-time env, or neither.
SOURCE_BUILD_ARG = "build_arg"
SOURCE_DEPLOY_ENV = "deploy_env"
SOURCE_NONE = "none"

# A git object name: 7-40 lowercase hex characters. Anything else (a branch
# name, an image reference, a template that never got substituted, an
# unexpanded ``${...}``) is rejected rather than reported as if it were a
# commit.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _read(source, name: str) -> str:
    raw = str(source.get(name) or "").strip().lower()
    return raw if _SHA_RE.match(raw) else ""


def _resolve(env: dict[str, str] | None) -> tuple[str, str]:
    source = os.environ if env is None else env
    baked = _read(source, BUILD_SHA_ENV)
    if baked:
        return baked, SOURCE_BUILD_ARG
    deployed = _read(source, DEPLOYED_SHA_ENV)
    if deployed:
        return deployed, SOURCE_DEPLOY_ENV
    return UNKNOWN_BUILD_SHA, SOURCE_NONE


def build_sha(env: dict[str, str] | None = None) -> str:
    """Return the commit SHA this process was built from, or ``"unknown"``.

    Never raises and never guesses: an absent, blank, or malformed value reads
    as ``UNKNOWN_BUILD_SHA`` so a deploy that cannot identify itself says so
    plainly instead of reporting a plausible-looking lie.
    """
    return _resolve(env)[0]


def build_sha_source(env: dict[str, str] | None = None) -> str:
    """Return which env var supplied ``build_sha()``.

    ``build_arg`` is the healthy answer on a deployed image. ``deploy_env`` on a
    real deploy means the image was built WITHOUT ``--build-arg
    VIOLA_BUILD_SHA``, so the SPA bundle also baked ``dev`` -- the #4811
    regression, visible from the same curl that reads the SHA.
    """
    return _resolve(env)[1]
