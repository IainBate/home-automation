"""Tests for scripts/daily_digest_check.py."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest import mock

import daily_digest_check as ddc
import pytest


def _write_log(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_groups_drops_singleton_and_keeps_recurring(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    now = datetime.now()
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    _write_log(
        logs_dir / "mg_saic_poller.log",
        [
            f"{recent},000 - src.api_clients.saic_client - WARNING - value out of range",
            f"{recent},000 - src.api_clients.saic_client - WARNING - value out of range",
            f"{recent},000 - src.api_clients.saic_client - WARNING - a one-off blip",
        ],
    )

    groups = ddc._read_groups(
        logs_dir, ["mg_saic_poller.log"], since=now - timedelta(days=1), min_occurrences=2
    )

    assert len(groups) == 1
    assert groups[0].message == "value out of range"
    assert groups[0].count == 2


def test_read_groups_ignores_old_entries(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    now = datetime.now()
    old = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    _write_log(
        logs_dir / "mg_saic_poller.log",
        [f"{old},000 - x - WARNING - too old" for _ in range(3)],
    )

    groups = ddc._read_groups(
        logs_dir, ["mg_saic_poller.log"], since=now - timedelta(days=1), min_occurrences=2
    )

    assert groups == []


def test_read_groups_missing_file_is_skipped(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    groups = ddc._read_groups(
        logs_dir, ["does_not_exist.log"], since=datetime.now() - timedelta(days=1), min_occurrences=2
    )

    assert groups == []


def test_build_digest_uses_config_values(tmp_path):
    with mock.patch.object(ddc, "get_project_root", return_value=str(tmp_path)):
        groups, lookback_days = ddc.build_digest({"log_lookback_days": 3, "min_occurrences": 5})

    assert lookback_days == 3
    assert groups == []


def test_main_dry_run_with_recurring_issue_does_not_send_email(capsys, monkeypatch):
    fake_config = {"daily_digest_check": {"enabled": True}}
    fake_group = ddc.IssueGroup("a.log", "WARNING", "flaky", 3, datetime(2026, 1, 1), datetime(2026, 1, 1))

    monkeypatch.setattr(sys, "argv", ["daily_digest_check.py", "--dry-run"])
    with mock.patch.object(ddc, "load_static_config", return_value=fake_config), mock.patch.object(
        ddc, "build_digest", return_value=([fake_group], 1)
    ), mock.patch.object(ddc, "send_email") as send_email, pytest.raises(SystemExit) as exc_info:
        ddc.main()

    assert exc_info.value.code == 0
    send_email.assert_not_called()
    assert "not sending email" in capsys.readouterr().out


def test_main_sends_email_when_issues_recur_and_not_dry_run(monkeypatch):
    fake_config = {"daily_digest_check": {"enabled": True}}
    fake_group = ddc.IssueGroup("a.log", "WARNING", "flaky", 3, datetime(2026, 1, 1), datetime(2026, 1, 1))

    monkeypatch.setattr(sys, "argv", ["daily_digest_check.py"])
    with mock.patch.object(ddc, "load_static_config", return_value=fake_config), mock.patch.object(
        ddc, "build_digest", return_value=([fake_group], 1)
    ), mock.patch.object(ddc, "send_email", return_value=True) as send_email, pytest.raises(
        SystemExit
    ) as exc_info:
        ddc.main()

    assert exc_info.value.code == 0
    send_email.assert_called_once()


def test_main_no_recurring_issues_skips_email(monkeypatch):
    fake_config = {"daily_digest_check": {"enabled": True}}

    monkeypatch.setattr(sys, "argv", ["daily_digest_check.py"])
    with mock.patch.object(ddc, "load_static_config", return_value=fake_config), mock.patch.object(
        ddc, "build_digest", return_value=([], 1)
    ), mock.patch.object(ddc, "send_email") as send_email, pytest.raises(SystemExit) as exc_info:
        ddc.main()

    assert exc_info.value.code == 0
    send_email.assert_not_called()


def test_main_disabled_skips_digest_entirely(monkeypatch):
    fake_config = {"daily_digest_check": {"enabled": False}}

    monkeypatch.setattr(sys, "argv", ["daily_digest_check.py"])
    with mock.patch.object(ddc, "load_static_config", return_value=fake_config), mock.patch.object(
        ddc, "build_digest"
    ) as build_digest, pytest.raises(SystemExit) as exc_info:
        ddc.main()

    assert exc_info.value.code == 0
    build_digest.assert_not_called()
