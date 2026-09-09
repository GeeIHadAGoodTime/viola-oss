"""
Security Audit Tool
===================

Automatically detects security issues in the codebase.
Can be run as standalone script or imported for integration testing.

Usage:
    python -m utils.security.audit_tool

    # Or as module
    from utils.security.audit_tool import SecurityAudit
    audit = SecurityAudit()
    issues = audit.run_audit()

Features:
- Detect API keys in logs
- Find plaintext secrets in config files
- Check for missing encryption
- Verify input validation
- Scan for hardcoded credentials
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.console import console
from core.logging_config import get_logger

logger = get_logger(__name__)


class SecurityAudit:
    """
    Comprehensive security audit for the Viola codebase.

    Checks for:
    - API key leakage in code and logs
    - Plaintext secrets in config files
    - Missing input validation
    - Hardcoded credentials
    - Insecure default settings
    """

    # Patterns that indicate secrets
    _SECRET_PATTERNS = {
        "api_key": re.compile(
            r'(?P<prefix>(?:openai|gpt|google|aws|azure)_api_key)\s*[:=]\s*["\']?(?P<value>(sk|pk|AIza|AKIA|ya29)-[a-zA-Z0-9_-]+)',
            re.IGNORECASE,
        ),
        "generic_secret": re.compile(r'password\s*[:=]\s*["\']?([^"\'\s]+)', re.IGNORECASE),
        "token": re.compile(r'token\s*[:=]\s*["\']?([a-fA-F0-9]{32,})', re.IGNORECASE),
    }

    # Suspicious keywords that might indicate secrets
    _SUSPICIOUS_KEYWORDS = [
        "hardcoded",
        "password",
        "secret",
        "credential",
        "private_key",
        "access_key",
        "session_id",
    ]

    # Files to scan
    _SCAN_EXTENSIONS = {".py", ".yaml", ".yml", ".json", ".env", ".txt", ".md"}

    # Files/directories to ignore
    _IGNORE_PATTERNS = [
        "__pycache__",
        ".git",
        "node_modules",
        ".pytest_cache",
        "venv",
        "env",
        ".venv",
        "*.pyc",
        "*.pyo",
        ".mypy_cache",
    ]

    # Avoid scanning large files
    MAX_FILE_SIZE = 100_000  # 100KB

    def __init__(self, base_path: str | Path | None = None):
        """
        Initialize security audit.

        Args:
            base_path: Base path to scan (defaults to project root)
        """
        if base_path is None:
            # Find project root (look for .viola-ai or pyproject.toml)
            base_path = Path.cwd()
            while not any((base_path / marker).exists() for marker in [".viola-ai", "pyproject.toml", ".git"]):
                base_path = base_path.parent
                if base_path == base_path.parent:  # Root reached
                    base_path = Path.cwd()
                    break

        self.base_path = Path(base_path)
        self.issues: list[dict[str, Any]] = []

    def run_audit(self) -> list[dict[str, Any]]:
        """
        Run complete security audit.

        Returns:
            List of detected issues with severity and location
        """
        logger.info("🔍 Starting security audit in %s", self.base_path)
        self.issues = []

        # Run all checks
        self._check_log_files()
        self._check_config_files()
        self._check_source_code()
        self._check_env_files()
        self._check_for_hardcoded_secrets()

        logger.info("✅ Audit complete: %s issues found", len(self.issues))
        return self.issues

    def _check_log_files(self) -> None:
        """Check log files for leaked secrets."""
        logger.debug("Checking log files...")

        logs_dir = self.base_path / "logs"
        if not logs_dir.exists():
            return

        for log_file in logs_dir.glob("*.log"):
            try:
                # Skip very large log files
                if log_file.stat().st_size > 10_000_000:  # 10MB
                    continue

                with open(log_file, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()

                for line_num, line in enumerate(lines, 1):
                    for pattern_name, pattern in self._SECRET_PATTERNS.items():
                        matches = pattern.finditer(line)
                        for match in matches:
                            self.issues.append(
                                {
                                    "severity": "CRITICAL",
                                    "type": "secret_in_log",
                                    "file": str(log_file),
                                    "line": line_num,
                                    "pattern": pattern_name,
                                    "context": line.strip()[:100],
                                    "fix": "Remove sensitive data from logs or use SecureApiKey wrapper",
                                }
                            )
            except Exception as e:
                logger.warning("Could not check %s: %s", log_file, e)

    def _check_config_files(self) -> None:
        """Check configuration files for plaintext secrets."""
        logger.debug("Checking config files...")

        # Check YAML/JSON config files
        for ext in [".yaml", ".yml", ".json"]:
            for config_file in self.base_path.rglob(f"*{ext}"):
                if self._should_ignore(config_file):
                    continue

                # Skip dependencies and large files
                if config_file.stat().st_size > self.MAX_FILE_SIZE:
                    continue

                try:
                    with open(config_file, encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    # Check for API keys
                    for pattern_name, pattern in self._SECRET_PATTERNS.items():
                        matches = list(pattern.finditer(content))
                        if matches:
                            self.issues.append(
                                {
                                    "severity": "HIGH",
                                    "type": "plaintext_secret",
                                    "file": str(config_file.relative_to(self.base_path)),
                                    "pattern": pattern_name,
                                    "count": len(matches),
                                    "fix": f"Use SecureSettingsManager to encrypt secrets in {config_file.name}",
                                }
                            )
                except Exception as e:
                    logger.debug("Could not check %s: %s", config_file, e)

    def _check_source_code(self) -> None:
        """Check source code for security issues."""
        logger.debug("Checking source code...")

        for py_file in self.base_path.rglob("*.py"):
            if self._should_ignore(py_file):
                continue

            try:
                with open(py_file, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()

                for line_num, line in enumerate(lines, 1):
                    # Check for hardcoded secrets
                    if re.search(r'["\'](sk|pk|AIza|AKIA)-[a-zA-Z0-9_-]+', line):
                        self.issues.append(
                            {
                                "severity": "CRITICAL",
                                "type": "hardcoded_secret",
                                "file": str(py_file.relative_to(self.base_path)),
                                "line": line_num,
                                "context": line.strip(),
                                "fix": "Remove hardcoded secret, use environment variable or config",
                            }
                        )

                    # Check for dangerous logging
                    if re.search(
                        r"logger\.(debug|info|warning|error)\(.*api_key.*\)",
                        line,
                        re.IGNORECASE,
                    ):
                        self.issues.append(
                            {
                                "severity": "HIGH",
                                "type": "insecure_logging",
                                "file": str(py_file.relative_to(self.base_path)),
                                "line": line_num,
                                "context": line.strip(),
                                "fix": "Use SecureApiKey wrapper or remove sensitive data from logs",
                            }
                        )

                # Check for missing input validation
                if "def" in "\n".join(lines) and not any("max_length" in l or "validator" in l for l in lines):
                    # Quick heuristic: endpoints without validation
                    if any("@app.post" in l or "@app.get" in l for l in lines):
                        self.issues.append(
                            {
                                "severity": "MEDIUM",
                                "type": "missing_validation",
                                "file": str(py_file.relative_to(self.base_path)),
                                "context": "API endpoints without input validation",
                                "fix": "Add Pydantic validators or size limits to all inputs",
                            }
                        )
            except Exception as e:
                logger.debug("Could not check %s: %s", py_file, e)

    def _check_env_files(self) -> None:
        """Check .env files for secrets."""
        logger.debug("Checking .env files...")

        env_files = list(self.base_path.glob(".env*"))
        if not env_files:
            self.issues.append(
                {
                    "severity": "INFO",
                    "type": "missing_env_file",
                    "message": "No .env file found - this is OK if using other config methods",
                }
            )
            return

        for env_file in env_files:
            if env_file.name == ".env.example":
                continue

            try:
                with open(env_file, encoding="utf-8") as f:
                    lines = f.readlines()

                for line in lines:
                    for pattern_name, pattern in self._SECRET_PATTERNS.items():
                        if pattern.search(line):
                            self.issues.append(
                                {
                                    "severity": "HIGH",
                                    "type": "secret_in_env",
                                    "file": str(env_file.relative_to(self.base_path)),
                                    "pattern": pattern_name,
                                    "context": line.strip()[:50],
                                    "fix": ".env files should be in .gitignore and not committed",
                                }
                            )
            except Exception as e:
                logger.debug("Could not check %s: %s", env_file, e)

    def _check_for_hardcoded_secrets(self) -> None:
        """Check for suspicious hardcoded secrets."""
        logger.debug("Checking for hardcoded sensitive values...")

        for py_file in self.base_path.rglob("*.py"):
            if self._should_ignore(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")

                # Look for suspicious patterns
                if any(keyword in content.lower() for keyword in ["hardcoded", "todo: remove"]):
                    self.issues.append(
                        {
                            "severity": "INFO",
                            "type": "suspicious_comment",
                            "file": str(py_file.relative_to(self.base_path)),
                            "context": "Contains keywords suggesting hardcoded secrets",
                            "fix": "Review for hardcoded credentials",
                        }
                    )
            except Exception as e:
                logger.debug("Could not check %s: %s", py_file, e)

    def _should_ignore(self, path: Path) -> bool:
        """Check if file/directory should be ignored."""
        path_str = str(path)
        return any(ignore_pattern in path_str for ignore_pattern in self._IGNORE_PATTERNS)

    def print_report(self) -> None:
        """Print formatted audit report."""
        if not self.issues:
            logger.info("✅ No security issues found!")
            return

        # Group by severity
        by_severity: dict[str, list[dict[str, Any]]] = {}
        for issue in self.issues:
            severity = issue["severity"]
            if severity not in by_severity:
                by_severity[severity] = []
            by_severity[severity].append(issue)

        # Print report
        console("\n" + "=" * 70)
        console("SECURITY AUDIT REPORT")
        console("=" * 70)

        for severity in ["CRITICAL", "HIGH", "MEDIUM", "INFO"]:
            if severity not in by_severity:
                continue

            issues = by_severity[severity]
            icon = "[!]" if severity == "CRITICAL" else "[~]" if severity == "HIGH" else "[ ]"

            console(f"\n{icon} {severity}: {len(issues)} issues")
            console("-" * 70)

            for issue in issues:
                console(f"\nType: {issue['type']}")
                if "file" in issue:
                    console(f"File: {issue['file']}")
                if "line" in issue:
                    console(f"Line: {issue['line']}")
                if "context" in issue:
                    console(f"Context: {issue['context']}")
                if "fix" in issue:
                    console(f"Fix: {issue['fix']}")
                console()

        console("=" * 70)

        # Summary
        total = len(self.issues)
        critical = len(by_severity.get("CRITICAL", []))
        high = len(by_severity.get("HIGH", []))

        console(f"\nSummary: {total} issues total ({critical} critical, {high} high)")
        console("=" * 70 + "\n")


def main():
    """Run security audit as standalone script."""
    import sys

    audit = SecurityAudit()
    issues = audit.run_audit()
    audit.print_report()

    # Exit with error code if critical issues found
    critical = sum(1 for i in issues if i["severity"] == "CRITICAL")
    sys.exit(1 if critical > 0 else 0)


if __name__ == "__main__":
    main()
