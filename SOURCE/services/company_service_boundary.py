"""Source-package boundary for optional Viola-operated services.

Personal desktop builds can run without the private company service packages.
Cloud and company-enabled builds declare those services required and fail
startup if one is absent. Package discovery only handles the entirely-absent
case: once a package is installed, its imports and route construction remain
fail-closed.
"""

from __future__ import annotations

from importlib.util import find_spec

from core.logging_config import get_logger

logger = get_logger(__name__)

_SHARED_PRIVATE_MODULES = frozenset(
    {
        "auth.desktop_gotrue_proxy",
        "auth.desktop_oauth_callback",
        "diagnostics.diagnostic_ingest_handler",
        "services.calendar.cloud_caldav",
        "telephony.cloud_routes",
    }
)


class RequiredCompanyServiceUnavailableError(RuntimeError):
    """A configured service surface lacks a required private module."""


def company_services_required() -> bool:
    """Return whether this process promises Viola-operated service surfaces."""
    from config.settings import settings

    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", surface) or surface).strip().lower()
    build_profile = str(getattr(settings, "build_profile", "personal") or "personal").strip().lower()
    return surface == "cloud" or deployment == "cloud" or build_profile != "personal"


def _root_package_absent(module_name: str) -> bool:
    root_package = module_name.partition(".")[0]
    try:
        return find_spec(root_package) is None
    except ModuleNotFoundError as exc:
        if exc.name == root_package:
            return True
        raise


def company_service_module_available(module_name: str, *, component: str) -> bool:
    """Return whether an optional private module can be registered.

    An entirely absent root package is an intentional standalone capability
    choice only on a personal desktop. A hosted or company-enabled process
    treats the same absence as fatal. If the root package exists, a missing
    submodule or any later import/registration error is always fatal; callers
    must not turn a broken installed company service into a silent omission.
    """
    if _root_package_absent(module_name):
        if company_services_required():
            raise RequiredCompanyServiceUnavailableError(
                "%s requires private module %s on this configured service surface" % (component, module_name)
            )
        logger.info("%s disabled: private package %s is not installed", component, module_name.partition(".")[0])
        return False

    if find_spec(module_name) is None:
        raise RequiredCompanyServiceUnavailableError(
            "%s cannot start: installed private package is missing module %s" % (component, module_name)
        )
    return True


def shared_company_service_available(module_name: str) -> bool:
    """Check a declared private component within an otherwise public namespace.

    Only an absent component is optional on a personal desktop. Installed-module
    import failures still propagate, and company-enabled surfaces require it.
    Local authentication is not optional and is deliberately absent from this set.
    """
    if module_name not in _SHARED_PRIVATE_MODULES:
        raise ValueError("Not a declared optional company component: " + module_name)
    if find_spec(module_name) is not None:
        return True
    if company_services_required():
        raise RequiredCompanyServiceUnavailableError("Configured company surface requires " + module_name)
    logger.info("Company component %s is not included in this source distribution", module_name)
    return False


__all__ = [
    "RequiredCompanyServiceUnavailableError",
    "company_service_module_available",
    "company_services_required",
    "shared_company_service_available",
]
