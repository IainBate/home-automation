"""Tests for claude_usage_poller.py's cache-write and stale-on-failure behavior."""

from __future__ import annotations

import json
from unittest import mock

import claude_usage_poller as poller


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
