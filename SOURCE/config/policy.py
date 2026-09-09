"""
Policy Configuration for Offline/Online Behavior

Defines policies for controlling offline vs cloud processing behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PolicyConfig:
    """Policy configuration for offline/online behavior"""

    offline_only: bool = False
    """If True, disable cloud components entirely"""

    degrade_gracefully: bool = True
    """If True, fallback to local when cloud unavailable"""

    cloud_accelerator: bool = True
    """If True, use cloud for faster/better quality when available"""

    max_local_latency_ms: int = 2000
    """Maximum acceptable latency for local processing (fallback to cloud if exceeded)"""

    def is_cloud_allowed(self) -> bool:
        """Check if cloud usage is allowed"""
        return not self.offline_only

    def should_use_cloud(self, local_latency_ms: int | None = None) -> bool:
        """
        Decide whether to use cloud based on policy

        Args:
            local_latency_ms: Measured local processing latency in milliseconds

        Returns:
            True if cloud should be used, False otherwise
        """
        if not self.is_cloud_allowed():
            return False

        if not self.cloud_accelerator:
            return False

        # If degrade_gracefully is enabled and local latency is too high, use cloud
        if local_latency_ms is not None:
            if self.degrade_gracefully and local_latency_ms > self.max_local_latency_ms:
                return True

        # Default: use cloud accelerator if enabled
        return self.cloud_accelerator

    def should_fallback_to_local(self, cloud_available: bool) -> bool:
        """
        Decide whether to fallback to local when cloud is unavailable

        Args:
            cloud_available: Whether cloud service is currently available

        Returns:
            True if should fallback to local, False if should fail
        """
        if self.offline_only:
            return True  # Always use local in offline-only mode

        if not cloud_available and self.degrade_gracefully:
            return True

        return False


def create_default_policy() -> PolicyConfig:
    """Create default policy configuration"""
    return PolicyConfig(
        offline_only=False,
        degrade_gracefully=True,
        cloud_accelerator=True,
        max_local_latency_ms=2000,
    )


def create_offline_only_policy() -> PolicyConfig:
    """Create offline-only policy (no cloud usage)"""
    return PolicyConfig(
        offline_only=True,
        degrade_gracefully=True,
        cloud_accelerator=False,
        max_local_latency_ms=5000,  # More lenient for offline-only
    )


def create_cloud_optimized_policy() -> PolicyConfig:
    """Create cloud-optimized policy (prefer cloud when available)"""
    return PolicyConfig(
        offline_only=False,
        degrade_gracefully=True,
        cloud_accelerator=True,
        max_local_latency_ms=1000,  # Stricter latency threshold
    )
