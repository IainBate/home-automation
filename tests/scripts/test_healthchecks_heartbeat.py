"""Tests for healthchecks_heartbeat.py's ping/disabled/failure behavior."""

from __future__ import annotations

from unittest import mock

import healthchecks_heartbeat as heartbeat
import requests


def test_run_returns_1_when_disabled(capsys):
    exit_code = heartbeat.run({"healthchecks_io": {"enabled": False}}, quiet=False)

    assert exit_code == 1
    assert "disabled" in capsys.readouterr().out


def test_run_pings_url_on_success():
    config = {"healthchecks_io": {"enabled": True, "ping_url": "https://hc-ping.com/abc123"}}

    with mock.patch.object(heartbeat.requests, "get") as mock_get:
        exit_code = heartbeat.run(config, quiet=True)

    assert exit_code == 0
    mock_get.assert_called_once_with("https://hc-ping.com/abc123", timeout=heartbeat.DEFAULT_TIMEOUT_SECONDS)


def test_run_pings_fail_suffix_on_request_failure():
    config = {"healthchecks_io": {"enabled": True, "ping_url": "https://hc-ping.com/abc123"}}

    with mock.patch.object(
        heartbeat.requests, "get", side_effect=requests.ConnectionError("no route to host")
    ) as mock_get:
        exit_code = heartbeat.run(config, quiet=True)

    assert exit_code == 1
    assert mock_get.call_args_list == [
        mock.call("https://hc-ping.com/abc123", timeout=heartbeat.DEFAULT_TIMEOUT_SECONDS),
        mock.call("https://hc-ping.com/abc123/fail", timeout=heartbeat.DEFAULT_TIMEOUT_SECONDS),
    ]


def test_run_survives_fail_ping_also_failing():
    config = {"healthchecks_io": {"enabled": True, "ping_url": "https://hc-ping.com/abc123"}}

    with mock.patch.object(heartbeat.requests, "get", side_effect=requests.ConnectionError("down")):
        exit_code = heartbeat.run(config, quiet=True)

    assert exit_code == 1
