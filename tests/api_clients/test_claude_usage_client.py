"""Tests for claude_usage_client.py - parsing, fallback shapes, and error handling.

Rate-limit safety note: this client is only ever meant to be called from
scripts/claude_usage_poller.py's slow cron cadence, never from the
dashboard's own fast poll loop - see that script's and this module's
docstrings. Nothing here tests cadence (that's cron config, not code), but
test_collect_claude_usage_* in test_status_collector.py confirms the
dashboard only ever reads a cached file, never calls this client directly.
"""

from __future__ import annotations

import json
from unittest import mock

from src.api_clients import claude_usage_client


def _fake_response(status_code=200, json_payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_payload or {}
    return response


def test_fetch_returns_none_when_disabled():
    assert claude_usage_client.fetch_claude_usage({"claude_usage": {"enabled": False}}) is None


def test_fetch_returns_none_when_access_token_missing():
    assert claude_usage_client.fetch_claude_usage({"claude_usage": {"enabled": True}}) is None


def test_fetch_parses_limits_list():
    payload = {
        "limits": [
            {"kind": "session", "percent": 42, "resets_at": "2026-09-01T15:00:00Z", "severity": "normal"},
            {"kind": "weekly_all", "percent": 71, "resets_at": "2026-09-07T00:00:00Z", "severity": "warning"},
        ]
    }
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with mock.patch.object(claude_usage_client.requests, "get", return_value=_fake_response(json_payload=payload)):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result["buckets"][0] == {
        "kind": "session",
        "label": "Current session (5-hour)",
        "percent_used": 42,
        "resets_at": "2026-09-01T15:00:00Z",
        "severity": "normal",
    }
    assert result["buckets"][1]["label"] == "This week - all models"
    assert result["extra_usage_percent"] is None


def test_fetch_falls_back_to_named_blocks_when_limits_absent():
    payload = {"five_hour": {"utilization": 30, "resets_at": "2026-09-01T15:00:00Z"}}
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with mock.patch.object(claude_usage_client.requests, "get", return_value=_fake_response(json_payload=payload)):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result["buckets"] == [
        {"kind": "session", "label": "Current session (5-hour)", "percent_used": 30, "resets_at": "2026-09-01T15:00:00Z", "severity": "normal"}
    ]


def test_fetch_includes_extra_usage_only_when_enabled():
    payload = {"limits": [], "extra_usage": {"is_enabled": True, "utilization": 12}}
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with mock.patch.object(claude_usage_client.requests, "get", return_value=_fake_response(json_payload=payload)):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result["extra_usage_percent"] == 12


def test_fetch_returns_none_on_unauthorized():
    config = {"claude_usage": {"enabled": True, "access_token": "expired-tok"}}
    with mock.patch.object(claude_usage_client.requests, "get", return_value=_fake_response(status_code=401)):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result is None


def test_fetch_returns_none_on_rate_limit():
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with mock.patch.object(claude_usage_client.requests, "get", return_value=_fake_response(status_code=429)):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result is None


def test_fetch_returns_none_on_network_error():
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with mock.patch.object(
        claude_usage_client.requests, "get", side_effect=claude_usage_client.requests.ConnectionError("boom")
    ):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result is None


def test_fetch_prefers_synced_token_state_over_bootstrap_config_value(tmp_path):
    """scripts/claude_usage_token_sync.py keeps this file fresher than the
    static secrets.yaml bootstrap value - same precedence as resideo_client.py.
    """
    import json  # noqa: PLC0415

    state_path = tmp_path / "claude_usage_token_state.json"
    state_path.write_text(json.dumps({"access_token": "synced-token"}), encoding="utf-8")

    config = {"claude_usage": {"enabled": True, "access_token": "bootstrap-token"}}

    with (
        mock.patch.object(claude_usage_client, "get_claude_usage_token_state_path", lambda: str(state_path)),
        mock.patch.object(claude_usage_client.requests, "get") as fake_get,
    ):
        fake_get.return_value = _fake_response(json_payload={"limits": []})
        claude_usage_client.fetch_claude_usage(config)

    assert fake_get.call_args.kwargs["headers"]["Authorization"] == "Bearer synced-token"


def test_fetch_falls_back_to_bootstrap_token_when_no_synced_state(tmp_path):
    config = {"claude_usage": {"enabled": True, "access_token": "bootstrap-token"}}

    with (
        mock.patch.object(
            claude_usage_client, "get_claude_usage_token_state_path", lambda: str(tmp_path / "missing.json")
        ),
        mock.patch.object(claude_usage_client.requests, "get") as fake_get,
    ):
        fake_get.return_value = _fake_response(json_payload={"limits": []})
        claude_usage_client.fetch_claude_usage(config)

    assert fake_get.call_args.kwargs["headers"]["Authorization"] == "Bearer bootstrap-token"
