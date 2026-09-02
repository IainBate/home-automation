"""Tests for scripts/weekly_health_check.py."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from unittest import mock

import pytest
import weekly_health_check as whc


def _write_log(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_log_issues_finds_recent_errors_across_files(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    now = datetime.now()
    recent_ts = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    old_ts = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    _write_log(
        logs_dir / "hotwater_mode_daemon.log",
        [
            f"{recent_ts},000 - hotwater_mode_daemon - ERROR - MELCloud failed",
            f"{recent_ts},000 - hotwater_mode_daemon - INFO - all fine",
        ],
    )
    _write_log(
        logs_dir / "battery_mode_daemon.log",
        [f"{old_ts},000 - battery_mode_daemon - ERROR - too old to count"],
    )

    issues = whc._read_log_issues(logs_dir, since=now - timedelta(days=7))

    assert len(issues) == 1
    assert issues[0].message == "MELCloud failed"
    assert issues[0].source_file == "hotwater_mode_daemon.log"


def test_read_log_issues_missing_dir_returns_empty(tmp_path):
    assert whc._read_log_issues(tmp_path / "does_not_exist", since=datetime.now()) == []


def test_check_service_no_systemctl_returns_unknown():
    with mock.patch.object(whc.shutil, "which", return_value=None):
        status = whc._check_service("home_automation.service")
    assert status.name == "home_automation.service"
    assert status.active is None


def test_check_service_active():
    with mock.patch.object(whc.shutil, "which", return_value="/usr/bin/systemctl"), mock.patch.object(
        whc.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode=0)
    ):
        status = whc._check_service("home_automation.service")
    assert status.active is True


def test_check_service_inactive():
    with mock.patch.object(whc.shutil, "which", return_value="/usr/bin/systemctl"), mock.patch.object(
        whc.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode=3)
    ):
        status = whc._check_service("home_automation.service")
    assert status.active is False


def test_check_service_timeout_is_unknown():
    with mock.patch.object(whc.shutil, "which", return_value="/usr/bin/systemctl"), mock.patch.object(
        whc.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 10)
    ):
        status = whc._check_service("home_automation.service")
    assert status.active is None


def test_build_report_combines_logs_and_services(tmp_path):
    with mock.patch.object(whc, "get_project_root", return_value=str(tmp_path)), mock.patch.object(
        whc, "_check_service", return_value=whc.ServiceStatus("a.service", True)
    ):
        report = whc.build_report({"systemd_services": ["a.service"], "log_lookback_days": 3})

    assert report.lookback_days == 3
    assert report.service_statuses == [whc.ServiceStatus("a.service", True)]
    assert report.log_issues == []


def test_main_dry_run_with_issues_does_not_send_email(capsys, monkeypatch):
    fake_config = {"weekly_health_check": {"enabled": True}}
    dirty_report = whc.HealthReport([], [whc.ServiceStatus("a.service", False)], 7)

    monkeypatch.setattr(sys, "argv", ["weekly_health_check.py", "--dry-run"])
    with mock.patch.object(whc, "load_static_config", return_value=fake_config), mock.patch.object(
        whc, "build_report", return_value=dirty_report
    ), mock.patch.object(whc, "send_email") as send_email, pytest.raises(SystemExit) as exc_info:
        whc.main()

    assert exc_info.value.code == 0
    send_email.assert_not_called()
    assert "not sending email" in capsys.readouterr().out


def test_main_sends_email_when_issues_found_and_not_dry_run(monkeypatch):
    fake_config = {"weekly_health_check": {"enabled": True}}
    dirty_report = whc.HealthReport([], [whc.ServiceStatus("a.service", False)], 7)

    monkeypatch.setattr(sys, "argv", ["weekly_health_check.py"])
    with mock.patch.object(whc, "load_static_config", return_value=fake_config), mock.patch.object(
        whc, "build_report", return_value=dirty_report
    ), mock.patch.object(whc, "send_email", return_value=True) as send_email, pytest.raises(
        SystemExit
    ) as exc_info:
        whc.main()

    assert exc_info.value.code == 0
    send_email.assert_called_once()


def test_main_disabled_skips_report_entirely(monkeypatch):
    fake_config = {"weekly_health_check": {"enabled": False}}

    monkeypatch.setattr(sys, "argv", ["weekly_health_check.py"])
    with mock.patch.object(whc, "load_static_config", return_value=fake_config), mock.patch.object(
        whc, "build_report"
    ) as build_report, pytest.raises(SystemExit) as exc_info:
        whc.main()

    assert exc_info.value.code == 0
    build_report.assert_not_called()
