"""Weekly Health Check - Log Scanning & Report Building (pure logic).

Shared by scripts/weekly_health_check.py, which does the actual I/O (reading
logs/*.log* files, running `systemctl is-active`) and hands the raw lines and
results in here to be parsed and turned into a report - kept separate so the
parsing/report-building rules are unit-testable without touching the
filesystem or a real systemd, mirroring this codebase's other
core_logic/*_logic.py modules (e.g. hotwater_decision_logic.py).

Design Principles:
- Pure functions: no side effects, no I/O, testable
- Clear data contracts: explicit input/output types using dataclasses
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# Matches the standard rotating-file format from
# src/daemon_support/base_daemon.py's setup_rotating_logger(), used by every
# daemon/long-running script in this project:
#   "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# with the default asctime format "YYYY-MM-DD HH:MM:SS,mmm". Deliberately
# does not attempt to capture multi-line traceback continuation lines that
# follow a logger.exception() call - those have no leading timestamp of
# their own and are skipped (parse_log_line returns None for them); the
# single summary line they're attached to is enough to flag the issue.
LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - "
    r"(?P<logger_name>\S+) - (?P<level>ERROR|CRITICAL) - (?P<message>.*)$"
)

# Same shape as LOG_LINE_PATTERN but also matches WARNING - used by
# scripts/daily_digest_check.py, which (unlike the weekly check above) cares
# about the WARNING-level failures logged by cron-driven pollers like
# claude_usage_poller.py and mg_saic_poller.py.
DAILY_DIGEST_LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - "
    r"(?P<logger_name>\S+) - (?P<level>WARNING|ERROR|CRITICAL) - (?P<message>.*)$"
)

LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class LogIssue:
    """One parsed ERROR/CRITICAL log line.

    Attributes:
        timestamp: When the log line was written (naive - log files don't
            carry timezone info, so this is compared only against other
            naive timestamps derived from the same local clock).
        logger_name: The logger that emitted it (e.g. "hotwater_mode_daemon").
        level: "ERROR" or "CRITICAL".
        message: The log message text.
        source_file: Bare filename it was read from (e.g.
            "battery_mode_daemon.log"), for grouping in the report.

    """

    timestamp: datetime
    logger_name: str
    level: str
    message: str
    source_file: str


def parse_log_line(
    line: str, source_file: str, *, pattern: re.Pattern[str] = LOG_LINE_PATTERN
) -> LogIssue | None:
    """Parse one log line into a LogIssue if it matches `pattern`, else None.

    Args:
        line: A single line from a rotating log file (trailing newline OK).
        source_file: Bare filename the line came from, attached to the result.
        pattern: Which levels count as an issue - LOG_LINE_PATTERN (default,
            ERROR/CRITICAL only, used by the weekly check) or
            DAILY_DIGEST_LOG_LINE_PATTERN (also matches WARNING, used by the
            daily digest).

    Returns:
        A LogIssue, or None if the line isn't a well-formed entry at a
        matching level (lower-level lines, traceback continuation lines,
        blank lines, or a malformed timestamp all return None).

    Examples:
        >>> issue = parse_log_line(
        ...     "2026-09-02 08:21:31,123 - hotwater_mode_daemon - ERROR - "
        ...     "MELCloud mode change failed after 4 attempts",
        ...     "hotwater_mode_daemon.log",
        ... )
        >>> issue.level
        'ERROR'
        >>> issue.logger_name
        'hotwater_mode_daemon'
        >>> issue.message
        'MELCloud mode change failed after 4 attempts'

        >>> parse_log_line(
        ...     "2026-09-02 08:21:31,123 - battery_mode_daemon - INFO - all good",
        ...     "battery_mode_daemon.log",
        ... ) is None
        True

        >>> parse_log_line("Traceback (most recent call last):", "x.log") is None
        True

    """
    match = pattern.match(line.rstrip("\n"))
    if match is None:
        return None
    try:
        timestamp = datetime.strptime(match.group("timestamp"), LOG_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return LogIssue(
        timestamp=timestamp,
        logger_name=match.group("logger_name"),
        level=match.group("level"),
        message=match.group("message"),
        source_file=source_file,
    )


def filter_recent_issues(issues: list[LogIssue], since: datetime) -> list[LogIssue]:
    """Return only the issues at/after `since`.

    Examples:
        >>> old = LogIssue(datetime(2026, 1, 1), "x", "ERROR", "old", "x.log")
        >>> new = LogIssue(datetime(2026, 9, 1), "x", "ERROR", "new", "x.log")
        >>> [i.message for i in filter_recent_issues([old, new], datetime(2026, 6, 1))]
        ['new']

    """
    return [issue for issue in issues if issue.timestamp >= since]


@dataclass(frozen=True)
class ServiceStatus:
    """Result of checking one systemd service.

    Attributes:
        name: The systemd unit name (e.g. "home_automation.service").
        active: True if `systemctl is-active` reported it running, False if
            it reported anything else (stopped, failed, ...), or None if it
            couldn't be checked at all (e.g. systemctl isn't available on
            this machine) - None is deliberately not treated as an issue,
            since it means "unknown", not "down".

    """

    name: str
    active: bool | None


@dataclass(frozen=True)
class HealthReport:
    """A complete weekly health check result, ready to report/email.

    Attributes:
        log_issues: ERROR/CRITICAL log lines found within the lookback window.
        service_statuses: One entry per configured systemd service.
        lookback_days: How many days of logs were scanned, for the report text.

    """

    log_issues: list[LogIssue]
    service_statuses: list[ServiceStatus]
    lookback_days: int

    @property
    def inactive_services(self) -> list[ServiceStatus]:
        """Services definitively found not active (excludes unknown/None)."""
        return [s for s in self.service_statuses if s.active is False]

    @property
    def has_issues(self) -> bool:
        """Whether this report contains anything worth alerting on.

        Examples:
            >>> HealthReport([], [ServiceStatus("a.service", True)], 7).has_issues
            False
            >>> HealthReport([], [ServiceStatus("a.service", False)], 7).has_issues
            True
            >>> issue = LogIssue(datetime(2026, 1, 1), "x", "ERROR", "boom", "x.log")
            >>> HealthReport([issue], [], 7).has_issues
            True

        """
        return bool(self.log_issues) or bool(self.inactive_services)

    def to_text(self) -> str:
        """Render this report as a plain-text summary (also used as the email body).

        Examples:
            >>> print(HealthReport([], [ServiceStatus("a.service", True)], 7).to_text())
            Weekly automation health check (last 7 days)
            <BLANKLINE>
            No significant issues found.
            <BLANKLINE>
            Services checked:
              OK a.service

        """
        lines = [f"Weekly automation health check (last {self.lookback_days} days)", ""]

        if not self.has_issues:
            lines.append("No significant issues found.")
        else:
            if self.inactive_services:
                lines.append(f"Services not running ({len(self.inactive_services)}):")
                lines.extend(f"  - {s.name}" for s in self.inactive_services)
                lines.append("")
            if self.log_issues:
                lines.append(f"Log errors/critical entries ({len(self.log_issues)}):")
                by_file: dict[str, list[LogIssue]] = {}
                for issue in self.log_issues:
                    by_file.setdefault(issue.source_file, []).append(issue)
                for source_file, file_issues in sorted(by_file.items()):
                    lines.append(f"  {source_file} ({len(file_issues)}):")
                    # Cap the sample shown per file - a stuck-in-a-loop failure
                    # can log the same error every poll cycle for days; a
                    # handful of examples is enough to identify the problem
                    # without the email becoming unreadable.
                    for issue in file_issues[:5]:
                        lines.append(f"    {issue.timestamp} [{issue.level}] {issue.message}")
                    if len(file_issues) > 5:
                        lines.append(f"    ... and {len(file_issues) - 5} more")

        if self.service_statuses:
            lines.append("")
            lines.append("Services checked:")
            for status in self.service_statuses:
                marker = "OK" if status.active else ("DOWN" if status.active is False else "??")
                lines.append(f"  {marker} {status.name}")

        return "\n".join(lines)
