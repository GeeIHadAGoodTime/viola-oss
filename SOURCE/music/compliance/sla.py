from __future__ import annotations

from dataclasses import dataclass, field

from core.logging_config import get_logger

from .telemetry import ProviderTelemetryRegistry, TelemetrySnapshot

logger = get_logger(__name__)


@dataclass(slots=True)
class SLAThresholds:
    """Configurable SLA thresholds per provider."""

    max_resolution_failure_rate: float = 0.05  # 5%
    max_resolution_p95_ms: float = 1500.0
    max_gap_ms: float = 75.0


@dataclass(slots=True)
class SLAStatus:
    """Result of an SLA evaluation."""

    provider: str
    healthy: bool
    breaches: list[str] = field(default_factory=list)
    snapshot: TelemetrySnapshot | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "healthy": self.healthy,
            "breaches": self.breaches,
            "snapshot": self.snapshot,
        }


class ProviderSLAMonitor:
    """Evaluate telemetry against SLA thresholds."""

    def __init__(
        self,
        telemetry: ProviderTelemetryRegistry,
        thresholds: SLAThresholds | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._thresholds = thresholds or SLAThresholds()

    def evaluate(self, provider: str) -> SLAStatus:
        snapshot = self._telemetry.snapshot(provider)
        breaches: list[str] = []

        # Failure rate
        failure_rate = snapshot.failure_rate
        if failure_rate > self._thresholds.max_resolution_failure_rate:
            breaches.append(
                f"resolution_failure_rate={failure_rate:.2%} exceeds {self._thresholds.max_resolution_failure_rate:.2%}"
            )

        # Latency
        p95 = snapshot.p95_resolution_ms or 0.0
        if p95 > self._thresholds.max_resolution_p95_ms:
            breaches.append(f"resolution_p95={p95}ms exceeds {self._thresholds.max_resolution_p95_ms}ms")

        # Gapless playback
        gap = snapshot.mean_gap_ms or 0.0
        if gap > self._thresholds.max_gap_ms:
            breaches.append(f"playback_gap_mean={gap}ms exceeds {self._thresholds.max_gap_ms}ms")

        status = SLAStatus(
            provider=provider,
            healthy=len(breaches) == 0,
            breaches=breaches,
            snapshot=snapshot,
        )

        if breaches:
            logger.warning("SLA breach detected for provider=%s: %s", provider, breaches)

        return status
