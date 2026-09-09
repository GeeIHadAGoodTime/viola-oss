"""``POST /v1/diagnostics/ui-error`` -- where an error in the desktop UI lands.

Why this route exists at all (2026-06-07 .. 2026-08-08, 62 days blind):
``ui/react-app/src/sentryClient.js`` reported browser errors by letting the
Sentry SDK POST an envelope straight to an ingest host. Two things made that a
guaranteed silent no-op on the desktop, one after the other with no gap:

* Until 2026-08-02 the DSN never reached the bundle (``vite.config.js`` did not
  pass ``VITE_SENTRY_DSN`` through ``define``), so ``initSentry()`` returned
  before initializing and the SDK sent nothing.
* From 2026-08-02 the SDK does initialize and does try to send -- and the
  desktop's own CSP (``ui/security/config.py``) refuses the request, because
  ``connect-src`` has never once contained an ingest origin
  (``git log -S"ingest" -- ui/security/config.py`` is empty). Proven live on
  2026-08-08: throwing a real ``TypeError`` in the running app fires
  ``securitypolicyviolation`` with ``violatedDirective: connect-src`` and
  ``disposition: enforce``.

Both failures are invisible from inside the product, which is the whole
problem: a reporter that cannot send also cannot report that it cannot send.

The fix is structural rather than a CSP entry. The browser now posts to the
desktop's OWN origin, which ``connect-src 'self'`` already permits and which no
future CSP edit can accidentally drop -- so this bug class cannot recur at the
transport layer. What happens next reuses the pipeline that already existed and
that the React app simply never used: the same allowlist sanitizer
(``diagnostics.diagnostic_minimum``), the same consent gates
(``diagnostics.diagnostic_consent``), the same spool and cloud relay
(``diagnostics.diagnostic_dispatch`` -> ``diagnostic_relay`` -> cloud
``/v1/diagnostics/ingest`` -> GlitchTip).

Two properties of this handler are load-bearing:

1. IT NEVER TRUSTS THE CLIENT. The body arrives over HTTP from a renderer that
   runs third-party-ish code (a YouTube iframe lives in the same app), so every
   field is rebuilt server-side through ``build_browser_diagnostic_minimum``.
   Nothing the client sends is forwarded verbatim -- exactly the stance
   ``diagnostic_ingest_handler.sanitize_ingested_minimum`` takes on the cloud
   side.

2. THE LOCAL WRITE IS UNCONDITIONAL; ONLY THE SEND IS GATED. Writing the error
   to this machine's own log is not a disclosure -- it is Tier 3, the user's own
   device, where Viola already logs its Python errors. So the log line happens
   before any consent check and regardless of its outcome, which means a UI
   error is visible to whoever is looking at that machine (a support bundle, a
   bug report, the founder reading logs) even while the cloud baseline is
   disarmed. Whether the anonymized copy may LEAVE the machine stays entirely
   with ``diagnostics_baseline_enabled`` + the opt-out, untouched by this route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Body, Request

if TYPE_CHECKING:
    from ui.api.context import ApiContext
    from ui.api.routes.common import RouteToolbox

log = get_logger(__name__)

ROUTE_PATH = "/v1/diagnostics/ui-error"

# A renderer can loop. Frames are bounded by the sanitizer; this bounds how many
# distinct reports one page load can push through the pipeline.
_MAX_FRAMES_ACCEPTED = 50

# The route is auth-exempt (see ui/security/config.py for why a crash report
# must not require a healthy session), so the loopback check IS its access
# control. Only the desktop's own renderer reaches it; a LAN spoke or anything
# else on the network cannot push fabricated diagnostics into the spool.
# ``::ffff:127.0.0.1`` is included deliberately: dual-stack uvicorn reports the
# IPv4-mapped form for a local peer, and omitting it is how a loopback check
# silently starts rejecting the very client it was written for.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "testclient"})


def is_loopback_client(request: Request | None) -> bool:
    """True when the request came from this machine's own renderer."""
    if request is None:
        return False
    host = getattr(getattr(request, "client", None), "host", None)
    if not host:
        return False
    text = str(host).strip().lower()
    if text in _LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 is all loopback, and Chromium/uvicorn have both been seen
    # reporting an address other than 127.0.0.1 from that range.
    return text.startswith("127.")


def handle_ui_error(body: Any) -> dict[str, Any]:
    """Log a desktop-UI error locally, then hand it to the consent-gated path.

    Never raises: this runs off the UI's error handler, and a diagnostics
    failure must not turn a recoverable render error into a second one.
    """
    from diagnostics.diagnostic_dispatch import dispatch_browser_crash_diagnostic
    from diagnostics.diagnostic_minimum import build_browser_diagnostic_minimum

    payload = body if isinstance(body, dict) else {}
    frames = payload.get("frames")
    if not isinstance(frames, list):
        frames = []

    # Build the anonymized minimum FIRST and log that, not the raw body: the
    # local log is the one place a UI error is guaranteed to land, and it should
    # not be the place where an unscrubbed message gets written to disk.
    minimum = build_browser_diagnostic_minimum(
        error_type=payload.get("error_type"),
        error_value=payload.get("error_value"),
        frames=frames[:_MAX_FRAMES_ACCEPTED],
        app_state=payload.get("app_state") if isinstance(payload.get("app_state"), dict) else None,
    )

    log.error(
        "Desktop UI error: %s: %s at %s (app_version=%s)",
        minimum.get("error_type"),
        minimum.get("error_value"),
        minimum.get("error_location") or {},
        minimum.get("app_version"),
    )

    disposition = dispatch_browser_crash_diagnostic(
        error_type=payload.get("error_type"),
        error_value=payload.get("error_value"),
        frames=frames[:_MAX_FRAMES_ACCEPTED],
        app_state=payload.get("app_state") if isinstance(payload.get("app_state"), dict) else None,
    )
    return {"recorded": True, "disposition": disposition}


def register_ui_error_route(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register ``POST /v1/diagnostics/ui-error`` on ``context.router``."""
    router = context.router

    @router.post(ROUTE_PATH)
    async def report_ui_error(request: Request, body: dict = Body(default={})):
        async def _inner():
            if not is_loopback_client(request):
                return JSONResponse(
                    status_code=403,
                    content=failure_response(
                        "ui_error_not_local",
                        "UI error reports are accepted from this device only.",
                    ),
                )
            return success_response(handle_ui_error(body))

        return await toolbox.record_and_call(_inner, route=ROUTE_PATH, method="POST")


__all__ = ["ROUTE_PATH", "handle_ui_error", "is_loopback_client", "register_ui_error_route"]
