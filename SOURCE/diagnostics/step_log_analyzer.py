"""Step Log Anomaly Detection for Agent-Tier Security Monitoring.

Analyzes agent step logs (logs/structured/agent-steps-YYYYMMDD.jsonl) to
detect suspicious patterns that may indicate prompt injection, data
exfiltration, or agent misuse.

Detected anomaly patterns:
    - Same tool called 10+ times in 30 seconds (loop/abuse)
    - File read of sensitive paths (~/.ssh, .env, credentials)
    - tool_search followed by DANGEROUS tool call
    - Credential-adjacent reads (password files, API key files)
    - Empty tool sets (tools_available_count: 0) indicating routing bugs

This module flags anomalies but does NOT block execution. It is designed
to run as a periodic check or on-demand diagnostic.

Usage:
    >>> from diagnostics.step_log_analyzer import StepLogAnalyzer
    >>> analyzer = StepLogAnalyzer()
    >>> anomalies = analyzer.analyze_recent(minutes=30)
    >>> for a in anomalies:
    ...     print(a["severity"], a["pattern"], a["details"])
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_logs_dir

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Anomaly Definitions
# ---------------------------------------------------------------------------

SENSITIVE_PATH_PATTERNS: list[str] = [
    ".ssh",
    ".gnupg",
    ".env",
    ".env.",
    "credentials",
    "secrets",
    "api_key",
    "apikey",
    "token",
    ".aws/credentials",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
    "shadow",
    "passwd",
    ".kube/config",
    "keychain",
    "keyring",
    "wallet.dat",
    "private_key",
    "secret_key",
]

DANGEROUS_TOOL_PATTERNS: set[str] = {
    "run_command",
    "write_file",
    "delete_file",
    "install_package",
    "register_mcp_server",
    "browser_run_script",
}

TOOL_BLOCKLIST_PATTERNS: list[str] = [
    "exec",
    "eval",
    "shell",
    "system",
    "subprocess",
]

# Thresholds
REPEATED_TOOL_THRESHOLD = 10  # same tool N times
REPEATED_TOOL_WINDOW_SECONDS = 30  # within N seconds
EMPTY_TOOLSET_THRESHOLD = 3  # N consecutive steps with 0 tools


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------


@dataclass
class Anomaly:
    """A detected anomaly in step log data."""

    severity: str  # "critical", "high", "medium", "low"
    pattern: str  # Short identifier for the anomaly type
    details: str  # Human-readable description
    timestamp: float  # When the anomaly was detected/occurred
    step_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "severity": self.severity,
            "pattern": self.pattern,
            "details": self.details,
            "timestamp": self.timestamp,
            "iso_time": datetime.fromtimestamp(self.timestamp, tz=UTC).isoformat(),
            "step_data": self.step_data,
        }


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class StepLogAnalyzer:
    """Analyzes agent step logs for security anomalies.

    Thread-safe. Can be run periodically or on-demand.
    """

    def __init__(self, log_dir: Path | str | None = None) -> None:
        if log_dir is None:
            # Default to project-relative structured log directory
            log_dir = get_logs_dir() / "structured"
        self._log_dir = Path(log_dir)

    def analyze_recent(self, minutes: int = 30) -> list[Anomaly]:
        """Analyze recent step logs for anomalies.

        Args:
            minutes: Look back this many minutes from now.

        Returns:
            List of detected anomalies, sorted by severity.
        """
        cutoff = time.time() - (minutes * 60)
        steps = self._load_steps(cutoff)

        if not steps:
            logger.debug("No step log entries found in last %d minutes", minutes)
            return []

        anomalies: list[Anomaly] = []

        anomalies.extend(self._detect_repeated_tools(steps))
        anomalies.extend(self._detect_sensitive_path_access(steps))
        anomalies.extend(self._detect_tool_search_escalation(steps))
        anomalies.extend(self._detect_credential_adjacent_reads(steps))
        anomalies.extend(self._detect_empty_toolsets(steps))
        anomalies.extend(self._detect_blocklist_tool_names(steps))

        # Sort by severity (critical first)
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        anomalies.sort(key=lambda a: severity_order.get(a.severity, 99))

        if anomalies:
            logger.warning(
                "Step log analysis found %d anomalies in last %d minutes",
                len(anomalies),
                minutes,
            )
        else:
            logger.debug("Step log analysis clean: no anomalies in last %d minutes", minutes)

        return anomalies

    def analyze_file(self, filepath: Path | str) -> list[Anomaly]:
        """Analyze a specific step log file.

        Args:
            filepath: Path to a JSONL step log file.

        Returns:
            List of detected anomalies.
        """
        steps = self._parse_jsonl(Path(filepath))
        if not steps:
            return []

        anomalies: list[Anomaly] = []
        anomalies.extend(self._detect_repeated_tools(steps))
        anomalies.extend(self._detect_sensitive_path_access(steps))
        anomalies.extend(self._detect_tool_search_escalation(steps))
        anomalies.extend(self._detect_credential_adjacent_reads(steps))
        anomalies.extend(self._detect_empty_toolsets(steps))
        anomalies.extend(self._detect_blocklist_tool_names(steps))

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        anomalies.sort(key=lambda a: severity_order.get(a.severity, 99))
        return anomalies

    # ------------------------------------------------------------------
    # Detection Methods
    # ------------------------------------------------------------------

    def _detect_repeated_tools(self, steps: list[dict[str, Any]]) -> list[Anomaly]:
        """Detect same tool called 10+ times within 30 seconds."""
        anomalies: list[Anomaly] = []

        # Group by task_id for per-task analysis
        task_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for step in steps:
            task_id = step.get("task_id", "unknown")
            task_groups[task_id].append(step)

        for task_id, task_steps in task_groups.items():
            # Sliding window: track tool calls with timestamps
            tool_timestamps: dict[str, list[float]] = defaultdict(list)

            for step in task_steps:
                tool_name = step.get("tool_name", "")
                ts = step.get("timestamp", 0)
                if not tool_name or not ts:
                    continue

                tool_timestamps[tool_name].append(ts)

                # Check if threshold exceeded within window
                recent = [t for t in tool_timestamps[tool_name] if ts - t <= REPEATED_TOOL_WINDOW_SECONDS]
                tool_timestamps[tool_name] = recent  # prune old entries

                if len(recent) >= REPEATED_TOOL_THRESHOLD:
                    anomalies.append(
                        Anomaly(
                            severity="high",
                            pattern="repeated_tool_call",
                            details=(
                                "Tool '%s' called %d times in %d seconds "
                                "(task: %s). Possible infinite loop or abuse."
                                % (
                                    tool_name,
                                    len(recent),
                                    REPEATED_TOOL_WINDOW_SECONDS,
                                    task_id[:12],
                                )
                            ),
                            timestamp=ts,
                            step_data={
                                "tool_name": tool_name,
                                "call_count": len(recent),
                                "task_id": task_id,
                            },
                        )
                    )
                    # Reset to avoid duplicate alerts for same burst
                    tool_timestamps[tool_name] = []

        return anomalies

    def _detect_sensitive_path_access(self, steps: list[dict[str, Any]]) -> list[Anomaly]:
        """Detect file reads targeting sensitive paths."""
        anomalies: list[Anomaly] = []

        file_read_tools = {"read_file", "list_directory", "file_info", "search_files"}

        for step in steps:
            tool_name = step.get("tool_name", "")
            if tool_name not in file_read_tools:
                continue

            tool_input = step.get("tool_input", {})
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except (json.JSONDecodeError, TypeError):
                    tool_input = {"raw": tool_input}

            # Check path argument
            path_value = str(tool_input.get("path", "") or tool_input.get("file", "") or "")
            path_lower = path_value.lower().replace("\\", "/")

            for pattern in SENSITIVE_PATH_PATTERNS:
                if pattern in path_lower:
                    anomalies.append(
                        Anomaly(
                            severity="critical",
                            pattern="sensitive_path_access",
                            details=(
                                "File operation '%s' targeting sensitive path: %s "
                                "(matched pattern: %s)" % (tool_name, path_value, pattern)
                            ),
                            timestamp=step.get("timestamp", time.time()),
                            step_data={
                                "tool_name": tool_name,
                                "path": path_value,
                                "pattern_matched": pattern,
                                "task_id": step.get("task_id", ""),
                            },
                        )
                    )
                    break  # One alert per step is sufficient

        return anomalies

    def _detect_tool_search_escalation(self, steps: list[dict[str, Any]]) -> list[Anomaly]:
        """Detect tool_search followed by DANGEROUS tool call.

        Pattern: agent searches for tools, then immediately calls a
        high-risk tool. May indicate reconnaissance before attack.
        """
        anomalies: list[Anomaly] = []

        for i in range(len(steps) - 1):
            current = steps[i]
            next_step = steps[i + 1]

            if current.get("tool_name") != "tool_search":
                continue

            next_tool = next_step.get("tool_name", "")
            if next_tool in DANGEROUS_TOOL_PATTERNS:
                anomalies.append(
                    Anomaly(
                        severity="medium",
                        pattern="tool_search_escalation",
                        details=(
                            "tool_search immediately followed by DANGEROUS tool '%s'. "
                            "Possible reconnaissance pattern." % next_tool
                        ),
                        timestamp=next_step.get("timestamp", time.time()),
                        step_data={
                            "search_input": current.get("tool_input", {}),
                            "escalated_tool": next_tool,
                            "task_id": next_step.get("task_id", ""),
                        },
                    )
                )

        return anomalies

    def _detect_credential_adjacent_reads(self, steps: list[dict[str, Any]]) -> list[Anomaly]:
        """Detect reads of files that commonly contain credentials."""
        anomalies: list[Anomaly] = []

        credential_filenames = {
            ".env",
            ".env.local",
            ".env.production",
            ".env.cloud",
            "credentials.json",
            "service_account.json",
            "keyfile.json",
            "config.json",  # Often contains API keys
            ".npmrc",
            ".pypirc",
            ".docker/config.json",
        }

        for step in steps:
            tool_name = step.get("tool_name", "")
            if tool_name not in ("read_file", "file_info"):
                continue

            tool_input = step.get("tool_input", {})
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except (json.JSONDecodeError, TypeError):
                    continue

            path_value = str(tool_input.get("path", "") or tool_input.get("file", "") or "")
            if not path_value:
                continue

            # Extract filename
            filename = Path(path_value).name.lower()
            if filename in credential_filenames:
                anomalies.append(
                    Anomaly(
                        severity="high",
                        pattern="credential_file_read",
                        details=("Agent read credential-adjacent file: %s " "(filename: %s)" % (path_value, filename)),
                        timestamp=step.get("timestamp", time.time()),
                        step_data={
                            "tool_name": tool_name,
                            "path": path_value,
                            "filename": filename,
                            "task_id": step.get("task_id", ""),
                        },
                    )
                )

        return anomalies

    def _detect_empty_toolsets(self, steps: list[dict[str, Any]]) -> list[Anomaly]:
        """Detect consecutive steps with zero tools available.

        This indicates a routing or registration bug, not a direct
        security threat, but it can cause the agent to fabricate
        information instead of using tools.
        """
        anomalies: list[Anomaly] = []

        consecutive_empty = 0
        for step in steps:
            count = step.get("tools_available_count")
            if count is not None and count == 0:
                consecutive_empty += 1
                if consecutive_empty >= EMPTY_TOOLSET_THRESHOLD:
                    anomalies.append(
                        Anomaly(
                            severity="medium",
                            pattern="empty_toolset",
                            details=(
                                "%d consecutive steps with 0 tools available. "
                                "Agent may fabricate information." % consecutive_empty
                            ),
                            timestamp=step.get("timestamp", time.time()),
                            step_data={
                                "consecutive_count": consecutive_empty,
                                "task_id": step.get("task_id", ""),
                            },
                        )
                    )
                    consecutive_empty = 0  # Reset after alert
            else:
                consecutive_empty = 0

        return anomalies

    def _detect_blocklist_tool_names(self, steps: list[dict[str, Any]]) -> list[Anomaly]:
        """Detect tool calls with blocklisted name patterns."""
        anomalies: list[Anomaly] = []

        for step in steps:
            tool_name = step.get("tool_name", "").lower()
            if not tool_name:
                continue

            for pattern in TOOL_BLOCKLIST_PATTERNS:
                if pattern in tool_name:
                    anomalies.append(
                        Anomaly(
                            severity="high",
                            pattern="blocklist_tool_name",
                            details=("Tool name '%s' matches blocklist pattern '%s'" % (tool_name, pattern)),
                            timestamp=step.get("timestamp", time.time()),
                            step_data={
                                "tool_name": tool_name,
                                "matched_pattern": pattern,
                                "task_id": step.get("task_id", ""),
                            },
                        )
                    )
                    break

        return anomalies

    # ------------------------------------------------------------------
    # Log Loading
    # ------------------------------------------------------------------

    def _load_steps(self, since_timestamp: float) -> list[dict[str, Any]]:
        """Load step log entries since a given timestamp.

        Reads today's log file (and yesterday's if needed for the
        time window). Steps are sorted by timestamp.
        """
        all_steps: list[dict[str, Any]] = []

        # Check today and yesterday's files
        today = datetime.fromtimestamp(time.time(), tz=UTC)
        yesterday = datetime.fromtimestamp(time.time() - 86400, tz=UTC)

        for dt in [yesterday, today]:
            filename = "agent-steps-%s.jsonl" % dt.strftime("%Y%m%d")
            filepath = self._log_dir / filename
            if filepath.exists():
                steps = self._parse_jsonl(filepath)
                all_steps.extend(s for s in steps if s.get("timestamp", 0) >= since_timestamp)

        all_steps.sort(key=lambda s: s.get("timestamp", 0))
        return all_steps

    def _parse_jsonl(self, filepath: Path) -> list[dict[str, Any]]:
        """Parse a JSONL file into a list of dicts."""
        steps: list[dict[str, Any]] = []
        try:
            with filepath.open("r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            steps.append(entry)
                    except json.JSONDecodeError:
                        logger.debug("Malformed JSON at %s:%d", filepath.name, line_num)
        except Exception as exc:
            logger.error("Failed to read step log %s: %s", filepath, exc)

        return steps


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def analyze_recent_steps(minutes: int = 30) -> list[dict[str, Any]]:
    """Quick-access function to analyze recent step logs.

    Returns anomalies as dicts for easy serialization.
    """
    analyzer = StepLogAnalyzer()
    anomalies = analyzer.analyze_recent(minutes)
    return [a.to_dict() for a in anomalies]
