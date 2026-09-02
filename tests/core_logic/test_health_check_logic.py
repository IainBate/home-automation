"""Unit tests for src/core_logic/health_check_logic.py."""

from __future__ import annotations

from datetime import datetime

from src.core_logic.health_check_logic import (
    HealthReport,
    LogIssue,
    ServiceStatus,
    filter_recent_issues,
    parse_log_line,
)


def test_parse_log_line_matches_error():
    issue = parse_log_line(
        "2026-09-02 08:21:31,123 - hotwater_mode_daemon - ERROR - something broke",
        "hotwater_mode_daemon.log",
    )
    assert issue == LogIssue(
        timestamp=datetime(2026, 9, 2, 8, 21, 31),
        logger_name="hotwater_mode_daemon",
        level="ERROR",
        message="something broke",
        source_file="hotwater_mode_daemon.log",
    )


def test_parse_log_line_matches_critical():
    issue = parse_log_line(
        "2026-09-02 08:21:31,123 - x - CRITICAL - very broke", "x.log"
    )
    assert issue is not None
    assert issue.level == "CRITICAL"


def test_parse_log_line_ignores_info_and_warning():
    assert parse_log_line("2026-09-02 08:21:31,123 - x - INFO - fine", "x.log") is None
    assert parse_log_line("2026-09-02 08:21:31,123 - x - WARNING - hmm", "x.log") is None


def test_parse_log_line_ignores_malformed_line():
    assert parse_log_line("Traceback (most recent call last):", "x.log") is None
    assert parse_log_line("", "x.log") is None
    assert parse_log_line("not a log line at all - ERROR - oops", "x.log") is None


def test_filter_recent_issues_excludes_older_than_since():
    old = LogIssue(datetime(2026, 1, 1), "x", "ERROR", "old", "x.log")
    new = LogIssue(datetime(2026, 9, 1), "x", "ERROR", "new", "x.log")

    result = filter_recent_issues([old, new], since=datetime(2026, 6, 1))

    assert result == [new]


def test_filter_recent_issues_boundary_is_inclusive():
    boundary = LogIssue(datetime(2026, 6, 1), "x", "ERROR", "boundary", "x.log")

    result = filter_recent_issues([boundary], since=datetime(2026, 6, 1))

    assert result == [boundary]


def test_health_report_no_issues_when_all_services_active_and_no_log_issues():
    report = HealthReport([], [ServiceStatus("a.service", True)], 7)
    assert report.has_issues is False
    assert report.inactive_services == []


def test_health_report_unknown_service_status_is_not_an_issue():
    report = HealthReport([], [ServiceStatus("a.service", None)], 7)
    assert report.has_issues is False


def test_health_report_inactive_service_is_an_issue():
    report = HealthReport([], [ServiceStatus("a.service", False)], 7)
    assert report.has_issues is True
    assert report.inactive_services == [ServiceStatus("a.service", False)]


def test_health_report_log_issue_is_an_issue():
    issue = LogIssue(datetime(2026, 1, 1), "x", "ERROR", "boom", "x.log")
    report = HealthReport([issue], [], 7)
    assert report.has_issues is True


def test_to_text_clean_report():
    text = HealthReport([], [ServiceStatus("a.service", True)], 7).to_text()
    assert "No significant issues found." in text
    assert "OK a.service" in text


def test_to_text_reports_inactive_service_and_log_issues():
    issue = LogIssue(datetime(2026, 1, 1, 9, 0, 0), "x", "ERROR", "boom", "x.log")
    report = HealthReport([issue], [ServiceStatus("a.service", False)], 7)

    text = report.to_text()

    assert "Services not running (1):" in text
    assert "a.service" in text
    assert "Log errors/critical entries (1):" in text
    assert "boom" in text
    assert "DOWN a.service" in text


def test_to_text_caps_sample_per_file_at_five():
    issues = [
        LogIssue(datetime(2026, 1, 1, 9, i, 0), "x", "ERROR", f"boom {i}", "x.log")
        for i in range(7)
    ]
    report = HealthReport(issues, [], 7)

    text = report.to_text()

    assert "boom 0" in text
    assert "boom 4" in text
    assert "boom 5" not in text
    assert "... and 2 more" in text
