"""
music.compliance
================

Compliance and resilience services for music providers.

Exports high-level helpers for:
- Audit logging (`MusicAuditPipeline`)
- Telemetry aggregation (`ProviderTelemetryRegistry`)
- SLA monitoring (`ProviderSLAMonitor`)
- Resilience coordination (`ResilienceCoordinator`)
- Service bootstrap helpers (`get_services`, `initialize_services`)
"""

from __future__ import annotations

from .audit_pipeline import AuditEvent, MusicAuditPipeline
from .resilience import PrefetchPool, ResilienceCoordinator
from .services import (
    MusicComplianceConfig,
    MusicComplianceServices,
    get_services,
    initialize_services,
)
from .sla import ProviderSLAMonitor, SLAStatus, SLAThresholds
from .telemetry import ProviderTelemetryRegistry, TelemetrySnapshot
from .youtube_tos import TOSComplianceResult, ViolationType, YouTubeMusicTOSEnforcer

__all__ = [
    "AuditEvent",
    "MusicAuditPipeline",
    "MusicComplianceConfig",
    "MusicComplianceServices",
    "PrefetchPool",
    "ProviderSLAMonitor",
    "ProviderTelemetryRegistry",
    "ResilienceCoordinator",
    "SLAStatus",
    "SLAThresholds",
    "TOSComplianceResult",
    "TelemetrySnapshot",
    "ViolationType",
    "YouTubeMusicTOSEnforcer",
    "get_services",
    "initialize_services",
]
