"""Tests for claude_usage_poller.py's cache-write and stale-on-failure behavior."""

from __future__ import annotations

import json
from unittest import mock

import claude_usage_poller as poller
from src.api_clients.claude_usage_client import RateLimited


def test_run_returns_1_when_disabled(capsys):
    exit_code = poller.run({"claude_usage": {"enabled": False}}, quiet=False)

    assert exit_code == 1
    assert "disabled" in capsys.readouterr().out


def test_run_writes_cache_on_success(tmp_path):
    usage_path = tmp_path / "claude_usage.json"
    usage = {"buckets": [{"kind": "session", "label": "x", "percent_used": 10, "resets_at": None, "severity": "normal"}], "extra_usage_percent": None}

    with (
        mock.patch.object(poller, "get_claude_usage_path", lambda: str(usage_path)),
        mock.patch.object(poller, "fetch_claude_usage", return_value=usage),
    ):
        exit_code = poller.run({"claude_usage": {"enabled": True}}, quiet=True)

    assert exit_code == 0
    saved = json.loads(usage_path.read_text(encoding="utf-8"))
    assert saved["buckets"] == usage["buckets"]
    assert "fetched_at" in saved


def test_run_leaves_previous_cache_in_place_on_failure(tmp_path):
    usage_path = tmp_path / "claude_usage.json"
    usage_path.write_text(json.dumps({"buckets": [], "extra_usage_percent": None, "fetched_at": "old"}), encoding="utf-8")

    with (
        mock.patch.object(poller, "get_claude_usage_path", lambda: str(usage_path)),
        mock.patch.object(poller, "fetch_claude_usage", return_value=None),
    ):
        exit_code = poller.run({"claude_usage": {"enabled": True}}, quiet=True)

    assert exit_code == 1
    saved = json.loads(usage_path.read_text(encoding="utf-8"))
    assert saved["fetched_at"] == "old"


def test_run_leaves_previous_cache_in_place_on_rate_limit_without_warning(tmp_path, caplog):
    """A 429 must not WARN - that's what makes cron mail Iain for a routine, self-healing backoff."""
    usage_path = tmp_path / "claude_usage.json"
    usage_path.write_text(json.dumps({"buckets": [], "extra_usage_percent": None, "fetched_at": "old"}), encoding="utf-8")

    with (
        mock.patch.object(poller, "get_claude_usage_path", lambda: str(usage_path)),
        mock.patch.object(poller, "fetch_claude_usage", return_value=RateLimited(retry_after_seconds=1350.0)),
        caplog.at_level("INFO"),
    ):
        exit_code = poller.run({"claude_usage": {"enabled": True}}, quiet=True)

    assert exit_code == 1
    saved = json.loads(usage_path.read_text(encoding="utf-8"))
    assert saved["fetched_at"] == "old"
    assert not any(record.levelname == "WARNING" for record in caplog.records)
    assert any("rate-limited" in record.message for record in caplog.records)


def test_run_logs_zero_retry_after_as_zero_not_unknown(tmp_path, caplog):
    """retry_after_seconds=0.0 is a real, valid value (retry immediately) - must not render as '?'."""
    usage_path = tmp_path / "claude_usage.json"
    usage_path.write_text(json.dumps({"buckets": [], "extra_usage_percent": None, "fetched_at": "old"}), encoding="utf-8")

    with (
        mock.patch.object(poller, "get_claude_usage_path", lambda: str(usage_path)),
        mock.patch.object(poller, "fetch_claude_usage", return_value=RateLimited(retry_after_seconds=0.0)),
        caplog.at_level("INFO"),
    ):
        poller.run({"claude_usage": {"enabled": True}}, quiet=True)

    assert any("retry after 0.0s" in record.message for record in caplog.records)
