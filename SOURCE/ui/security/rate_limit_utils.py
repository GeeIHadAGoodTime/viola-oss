from __future__ import annotations

from types import MethodType

from fastapi.routing import APIRoute

from fastapi import APIRouter

RATE_LIMIT_VALUE_ATTR = "_rate_limit_value"
RATE_LIMIT_EXPLICIT_ATTR = "_rate_limit_explicit"
_DEFAULT_RATE_LIMIT_ATTR = "_default_rate_limit"
_LIMITER_ENABLED_ATTR = "_limiter_enabled"
_RATE_LIMIT_PATCH_INSTALLED_ATTR = "_rate_limit_patch_installed"


def _set_route_rate_limit(route: APIRoute, value: str | None, explicit: bool) -> None:
    setattr(route, RATE_LIMIT_EXPLICIT_ATTR, explicit)
    setattr(route, RATE_LIMIT_VALUE_ATTR, value)


def configure_router_rate_limits(router: APIRouter, *, default_limit: str | None, limiter_enabled: bool) -> None:
    """
    Track per-route rate limit metadata for later enforcement.
    """
    setattr(router, _DEFAULT_RATE_LIMIT_ATTR, default_limit)
    setattr(router, _LIMITER_ENABLED_ATTR, limiter_enabled)

    def update_existing_routes() -> None:
        current_default = getattr(router, _DEFAULT_RATE_LIMIT_ATTR, None)
        enabled = getattr(router, _LIMITER_ENABLED_ATTR, False)
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            explicit = bool(getattr(route, RATE_LIMIT_EXPLICIT_ATTR, False))
            if not enabled:
                limit_value = None
            elif explicit:
                limit_value = getattr(route, RATE_LIMIT_VALUE_ATTR, None)
            else:
                limit_value = current_default
            _set_route_rate_limit(route, limit_value, explicit)

    update_existing_routes()

    if getattr(router, _RATE_LIMIT_PATCH_INSTALLED_ATTR, False):
        return

    original_add_api_route = router.add_api_route

    def add_api_route_with_limits(
        self: APIRouter,
        path: str,
        endpoint,
        *,
        rate_limit: str | None = None,
        rate_limit_disabled: bool = False,
        **kwargs,
    ):
        default_value = getattr(self, _DEFAULT_RATE_LIMIT_ATTR, None)
        enabled = getattr(self, _LIMITER_ENABLED_ATTR, False)

        explicit = rate_limit is not None or rate_limit_disabled
        if not enabled:
            limit_value: str | None = None
        elif rate_limit_disabled:
            limit_value = None
        elif rate_limit is not None:
            limit_value = rate_limit
        else:
            limit_value = default_value

        result = original_add_api_route(path, endpoint, **kwargs)

        if self.routes:
            route = self.routes[-1]
            if isinstance(route, APIRoute):
                _set_route_rate_limit(route, limit_value, explicit)

        return result

    object.__setattr__(router, "add_api_route", MethodType(add_api_route_with_limits, router))
    setattr(router, _RATE_LIMIT_PATCH_INSTALLED_ATTR, True)


def get_route_rate_limit(route: APIRoute) -> str | None:
    """Return the effective rate-limit string for a registered route (if any)."""
    return getattr(route, RATE_LIMIT_VALUE_ATTR, None)


__all__ = ["configure_router_rate_limits", "get_route_rate_limit"]
