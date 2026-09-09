from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from .audit_pipeline import MusicAuditPipeline
from .resilience import ResilienceCoordinator
from .sla import ProviderSLAMonitor, SLAThresholds
from .telemetry import ProviderTelemetryRegistry


@dataclass(slots=True)
class MusicComplianceConfig:
    """Configuration for compliance services."""

    sla_thresholds: SLAThresholds = field(default_factory=SLAThresholds)


@dataclass(slots=True)
class MusicComplianceServices:
    """Bundle of compliance-related services."""

    audit: MusicAuditPipeline
    telemetry: ProviderTelemetryRegistry
    sla_monitor: ProviderSLAMonitor
    resilience: ResilienceCoordinator


_services: MusicComplianceServices | None = None
_lock = Lock()


def initialize_services(
    config: MusicComplianceConfig | None = None,
) -> MusicComplianceServices:
    """Initialize and return compliance services singleton."""
    global _services
    with _lock:
        if _services is not None:
            return _services
        cfg = config or MusicComplianceConfig()
        telemetry = ProviderTelemetryRegistry()
        services = MusicComplianceServices(
            audit=MusicAuditPipeline(),
            telemetry=telemetry,
            sla_monitor=ProviderSLAMonitor(telemetry, cfg.sla_thresholds),
            resilience=ResilienceCoordinator(),
        )
        _services = services
        return services


def get_services() -> MusicComplianceServices:
    """Get existing services, initializing if needed."""
    global _services
    if _services is None:
        return initialize_services()
    return _services
