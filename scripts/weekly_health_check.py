#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Weekly Automation Health Check (one-shot CLI).

Scans logs/*.log* (the current file plus any midnight-rotated backups within
the lookback window - see src/daemon_support/base_daemon.py's
setup_rotating_logger, used by every daemon/long-running script here) for
ERROR/CRITICAL entries, and checks that the configured systemd services are
active. Only emails a summary (via src/utils/emailer.py) when something is
actually wrong - a clean week produces no email, only the printed report.

Repeated automation/hardware failures aren't checked separately: this
project's Circuit Breaker convention (see CLAUDE.md) already logs those at
ERROR level wherever they happen (e.g. "MELCloud mode change ... failed after
N attempts", a hardware client's broad `except Exception` handler), so the
log scan above already covers them without a second, parallel mechanism to
keep in sync.

Usage:
    python3 scripts/weekly_health_check.py                # check and email if needed
    python3 scripts/weekly_health_check.py --dry-run       # check and print only
    python3 scripts/weekly_health_check.py --quiet         # suppress the printed report

Intended to run weekly via cron - see config.yaml's weekly_health_check:
comments for an example crontab line.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Any

from src.config_manager.config_manager import load_static_config
from src.core_logic.health_check_logic import (
    HealthReport,
    LogIssue,
    ServiceStatus,
    filter_recent_issues,
    parse_log_line,
)
from src.utils.emailer import send_email
from src.utils.paths import get_project_root

DEFAULT_LOG_LOOKBACK_DAYS = 7
SYSTEMCTL_TIMEOUT_SECONDS = 10


def get_config_path() -> str:
    """Resolve config.yaml relative to the project root, not the process cwd."""
    return str(Path(get_project_root()) / "config.yaml")


def _read_log_issues(logs_dir: Path, since: datetime) -> list[LogIssue]:
    """Read every logs/*.log* file and return ERROR/CRITICAL entries at/after `since`."""
    issues: list[LogIssue] = []
    if not logs_dir.is_dir():
        return issues

    for log_file in sorted(logs_dir.glob("*.log*")):
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            issue = parse_log_line(line, log_file.name)
            if issue is not None:
                issues.append(issue)

    return filter_recent_issues(issues, since)


def _check_service(service_name: str) -> ServiceStatus:
    """Check one systemd service via `systemctl is-active`.

    Returns active=None (unknown, not an issue) if systemctl isn't available
    at all - this project develops on macOS but deploys to a systemd-based
    Pi, so this check must degrade gracefully rather than reporting every
    service "down" on a dev machine.
    """
    if shutil.which("systemctl") is None:
        return ServiceStatus(service_name, None)

    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ServiceStatus(service_name, None)

    return ServiceStatus(service_name, result.returncode == 0)


def build_report(hc_config: dict[str, Any]) -> HealthReport:
    """Gather log issues and service statuses into a HealthReport."""
    lookback_days = hc_config.get("log_lookback_days", DEFAULT_LOG_LOOKBACK_DAYS)
    since = datetime.now() - timedelta(days=lookback_days)  # noqa: DTZ005 - log timestamps are local-naive

    logs_dir = Path(get_project_root()) / "logs"
    log_issues = _read_log_issues(logs_dir, since)

    service_names = hc_config.get("systemd_services", [])
    service_statuses = [_check_service(name) for name in service_names]

    return HealthReport(log_issues, service_statuses, lookback_days)


def main() -> None:
    """Execute main entry point."""
    parser = argparse.ArgumentParser(
        description="Scan logs and systemd services for the past week's issues, "
        "and email a summary if anything is wrong",
        epilog="Examples:\n"
        "  python3 scripts/weekly_health_check.py\n"
        "  python3 scripts/weekly_health_check.py --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Build and print the report, but don't send email"
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress the printed report")
    args = parser.parse_args()

    config = load_static_config(args.config or get_config_path())
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    hc_config = config.get("weekly_health_check", {})
    if not hc_config.get("enabled", True):
        if not args.quiet:
            print("Weekly health check is disabled (weekly_health_check.enabled: false)")
        sys.exit(0)

    report = build_report(hc_config)

    if not args.quiet:
        print(report.to_text())

    if not report.has_issues:
        sys.exit(0)

    if args.dry_run:
        if not args.quiet:
            print("\n(dry run - not sending email)")
        sys.exit(0)

    sent = send_email(
        config,
        subject=(
            f"Home automation: {len(report.log_issues)} log issue(s), "
            f"{len(report.inactive_services)} service(s) down"
        ),
        body=report.to_text(),
    )
    # Exit 0 even when issues were found and reported (by print and/or email)
    # - a nonzero exit here would make cron mail its own failure notice on
    # top of (or instead of, if email itself failed) this script's own
    # alert, which is confusing rather than helpful. Only a genuine inability
    # to run the check at all (config load failure, above) exits nonzero.
    if not sent and not args.quiet:
        print("\nIssues found, but the alert email could not be sent (see logs above)")
    sys.exit(0)


if __name__ == "__main__":
    main()
