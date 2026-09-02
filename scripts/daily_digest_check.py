#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Daily Digest Check for cron-driven pollers (one-shot CLI).

claude_usage_poller.py and mg_saic_poller.py run unattended via cron, many
times a day, and log their failures to their own rotating file instead of
stderr (see src/utils/logging_setup.py's configure_cron_safe_logging) - so a
transient hiccup no longer makes cron mail an email on the spot. This script
is what replaces that: once a day, it scans those pollers' log files for
WARNING/ERROR/CRITICAL entries, drops anything that occurred only once in the
window (a single blip - see health_check_logic.squelch_transient), and
emails a summary (via src/utils/emailer.py) of what's left, most-important
first, ONLY if something actually recurred - a clean day produces no email.

This is deliberately separate from scripts/weekly_health_check.py, which
covers the battery/hot-water daemons (systemd services, checked weekly,
ERROR/CRITICAL only) - those are the priority subsystems and already have
their own faster path via service-down detection; this script's job is
purely to stop low-priority "web service" API pollers from spamming an
email per poll cycle.

Usage:
    python3 scripts/daily_digest_check.py                # check and email if needed
    python3 scripts/daily_digest_check.py --dry-run       # check and print only
    python3 scripts/daily_digest_check.py --quiet         # suppress the printed report

Intended to run daily via cron - see config.yaml's daily_digest_check:
comments for an example crontab line.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import datetime, timedelta
from typing import Any

from src.config_manager.config_manager import load_static_config
from src.core_logic.health_check_logic import (
    DAILY_DIGEST_LOG_LINE_PATTERN,
    IssueGroup,
    filter_recent_issues,
    group_issues,
    parse_log_line,
    render_digest_text,
    squelch_transient,
)
from src.utils.emailer import send_email
from src.utils.paths import get_project_root

DEFAULT_LOG_LOOKBACK_DAYS = 1
DEFAULT_MIN_OCCURRENCES = 2
DEFAULT_LOG_FILES = ["claude_usage_poller.log", "mg_saic_poller.log"]


def get_config_path() -> str:
    """Resolve config.yaml relative to the project root, not the process cwd."""
    return str(Path(get_project_root()) / "config.yaml")


def _read_groups(logs_dir: Path, log_files: list[str], since: datetime, min_occurrences: int) -> list[IssueGroup]:
    """Read the named poller logs and return squelched, priority-sorted issue groups."""
    issues = []
    for log_filename in log_files:
        log_file = logs_dir / log_filename
        if not log_file.is_file():
            continue
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            issue = parse_log_line(line, log_file.name, pattern=DAILY_DIGEST_LOG_LINE_PATTERN)
            if issue is not None:
                issues.append(issue)

    recent = filter_recent_issues(issues, since)
    return squelch_transient(group_issues(recent), min_occurrences)


def build_digest(digest_config: dict[str, Any]) -> tuple[list[IssueGroup], int]:
    """Gather squelched issue groups for the configured lookback window. Returns (groups, lookback_days)."""
    lookback_days = digest_config.get("log_lookback_days", DEFAULT_LOG_LOOKBACK_DAYS)
    min_occurrences = digest_config.get("min_occurrences", DEFAULT_MIN_OCCURRENCES)
    log_files = digest_config.get("log_files", DEFAULT_LOG_FILES)
    since = datetime.now() - timedelta(days=lookback_days)  # noqa: DTZ005 - log timestamps are local-naive

    logs_dir = Path(get_project_root()) / "logs"
    groups = _read_groups(logs_dir, log_files, since, min_occurrences)

    return groups, lookback_days


def main() -> None:
    """Execute main entry point."""
    parser = argparse.ArgumentParser(
        description="Scan cron-driven poller logs for the past day's recurring issues, "
        "and email a summary if anything recurred",
        epilog="Examples:\n"
        "  python3 scripts/daily_digest_check.py\n"
        "  python3 scripts/daily_digest_check.py --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Build and print the digest, but don't send email"
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress the printed report")
    args = parser.parse_args()

    config = load_static_config(args.config or get_config_path())
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    digest_config = config.get("daily_digest_check", {})
    if not digest_config.get("enabled", True):
        if not args.quiet:
            print("Daily digest check is disabled (daily_digest_check.enabled: false)")
        sys.exit(0)

    groups, lookback_days = build_digest(digest_config)
    report_text = render_digest_text(groups, lookback_days)

    if not args.quiet:
        print(report_text)

    if not groups:
        sys.exit(0)

    if args.dry_run:
        if not args.quiet:
            print("\n(dry run - not sending email)")
        sys.exit(0)

    sent = send_email(
        config,
        subject=f"Home automation: {len(groups)} recurring web-service issue(s) today",
        body=report_text,
    )
    # Exit 0 even when issues were found and reported - a nonzero exit here
    # would make cron mail its own failure notice on top of (or instead of,
    # if email itself failed) this script's own alert. Only a genuine
    # inability to run the check at all (config load failure, above) exits
    # nonzero.
    if not sent and not args.quiet:
        print("\nIssues found, but the alert email could not be sent (see logs above)")
    sys.exit(0)


if __name__ == "__main__":
    main()
