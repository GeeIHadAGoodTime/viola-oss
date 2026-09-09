"""Container-aware effective CPU count.

``os.cpu_count()`` (and affinity-based tools like ``nproc``) report the HOST's
core count from inside a Docker container even when the container is capped
below that with ``--cpus`` / docker-compose's ``deploy.resources.limits.cpus``.
That limit is enforced through the Linux CFS bandwidth controller (cgroup
``cpu.max`` on the unified v2 hierarchy, or the split ``cpu.cfs_quota_us`` /
``cpu.cfs_period_us`` pair on v1) -- it restricts how much CPU TIME the
container's cgroup may consume, not which CPUs are visible to the process.
Docker never narrows the CPU affinity mask or ``/proc/cpuinfo`` to match
``--cpus``, so anything that sizes a worker pool from ``os.cpu_count()``
inside such a container oversubscribes it by the host/container ratio.

For example, a container limited to half its host's CPU capacity can still
report the full host CPU count. Reading the cgroup quota avoids sizing its
worker pool from that unrestricted count.

``effective_cpu_count()`` fixes the class for every future container/host
combination by reading the process's OWN cgroup CPU quota and clamping the
host CPU count down to it when a quota is actually set. Everywhere the
process is NOT under a sub-host quota (desktop, macOS, Windows, a CI runner
with no ``--cpus``, a container run without ``--cpus``) this returns exactly
what ``os.cpu_count()`` would have -- there is no behavior change outside the
exact scenario this exists to fix.
"""

from __future__ import annotations

import math
import os
import sys

_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_QUOTA_US = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_PERIOD_US = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

# Absorbs float noise from the quota/period division (e.g. 800000/100000
# landing at 7.999999999999999 rather than 8.0) without changing genuine
# fractional quotas -- 2.5 CPUs must still floor to 2, not round up to 3.
_QUOTA_EPSILON = 1e-9


def _cgroup_v2_quota_cpus(path: str = _CGROUP_V2_CPU_MAX) -> float | None:
    """Parse cgroup v2's unified ``cpu.max`` (``"$MAX $PERIOD"`` or ``"max $PERIOD"``).

    Returns the fractional CPU quota (e.g. ``8.0`` for an 8-CPU cap), or
    ``None`` when unset (``max``), unreadable, or malformed. Never raises.
    """
    try:
        with open(path, encoding="ascii") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _cgroup_v1_quota_cpus(
    quota_path: str = _CGROUP_V1_QUOTA_US,
    period_path: str = _CGROUP_V1_PERIOD_US,
) -> float | None:
    """Parse cgroup v1's split ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us`` pair.

    ``cfs_quota_us`` reads ``-1`` when unset (unlimited). Returns ``None`` on
    that, on unreadable/malformed files, or on a non-positive period. Never
    raises.
    """
    try:
        with open(quota_path, encoding="ascii") as handle:
            quota = int(handle.read().strip())
        with open(period_path, encoding="ascii") as handle:
            period = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _quota_cpus() -> float | None:
    """Best-effort fractional CPU quota from this process's cgroup, v2 then v1.

    Linux-only (the CFS bandwidth controller this reads is a Linux kernel /
    cgroup feature); returns ``None`` immediately on any other platform so
    desktop (Windows/macOS) behavior never changes.
    """
    if sys.platform != "linux":
        return None
    quota = _cgroup_v2_quota_cpus()
    if quota is not None:
        return quota
    return _cgroup_v1_quota_cpus()


def effective_cpu_count(default: int = 1) -> int:
    """CPU count clamped to this process's cgroup CPU quota when one is set.

    Identical to ``os.cpu_count() or default`` everywhere the process is not
    under a sub-host CPU quota. Inside a quota-capped container (Docker
    ``--cpus`` / compose ``deploy.resources.limits.cpus`` / a Kubernetes CPU
    limit) returns the quota rounded DOWN to a whole core (minimum 1) --
    floor, not ceil, because callers size worker pools against a hard
    wall-clock turn budget (#4433: the phone STT pool's 2.4s budget), and
    rounding up would still oversubscribe the last fractional core. Never
    returns more than the host's own ``os.cpu_count()``, so a misconfigured
    quota larger than the host can never make sizing worse than today's
    behavior.

    Not cached: the two cgroup file reads are microsecond-cheap pseudo-file
    reads (not a hot inner loop -- called at most once per STT model
    construction / transcribe-pool sizing), and callers plus their tests
    already expect a fresh read every call (mirrors the existing
    ``_phone_stt_cpu_threads()`` / ``_phone_stt_max_concurrent()`` idiom in
    ``telephony/call_manager.py``, which re-reads its env var on every call).
    """
    host = os.cpu_count() or default
    quota = _quota_cpus()
    if quota is None:
        return host
    capped = math.floor(quota + _QUOTA_EPSILON)
    return max(1, min(capped, host))
