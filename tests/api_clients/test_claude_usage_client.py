"""Tests for claude_usage_client.py - parsing, fallback shapes, and error handling.

Rate-limit safety note: this client is only ever meant to be called from
scripts/claude_usage_poller.py's slow cron cadence, never from the
dashboard's own fast poll loop - see that script's and this module's
docstrings. Nothing here tests cadence (that's cron config, not code), but
test_collect_claude_usage_* in test_status_collector.py confirms the
dashboard only ever reads a cached file, never calls this client directly.

Isolation note: _read_local_claude_code_access_token() reads THIS machine's
real Keychain/credentials file when not mocked - every test below that
exercises fetch_claude_usage() mocks it explicitly (usually to None), so
these tests give the same result whether or not the machine running them
happens to have `claude` logged in.
"""

from __future__ import annotations

from unittest import mock

from src.api_clients import claude_usage_client


def _fake_response(status_code=200, json_payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_payload or {}
    return response


def _no_local_token():
    return mock.patch.object(claude_usage_client, "_read_local_claude_code_access_token", return_value=None)


def test_fetch_returns_none_when_disabled():
    assert claude_usage_client.fetch_claude_usage({"claude_usage": {"enabled": False}}) is None


def test_fetch_returns_none_when_no_token_available_anywhere():
    with _no_local_token():
        assert claude_usage_client.fetch_claude_usage({"claude_usage": {"enabled": True}}) is None


def test_fetch_parses_limits_list():
    payload = {
        "limits": [
            {"kind": "session", "percent": 42, "resets_at": "2026-09-01T15:00:00Z", "severity": "normal"},
            {"kind": "weekly_all", "percent": 71, "resets_at": "2026-09-07T00:00:00Z", "severity": "warning"},
        ]
    }
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with _no_local_token(), mock.patch.object(
        claude_usage_client.requests, "get", return_value=_fake_response(json_payload=payload)
    ):
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
    with _no_local_token(), mock.patch.object(
        claude_usage_client.requests, "get", return_value=_fake_response(json_payload=payload)
    ):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result["buckets"] == [
        {"kind": "session", "label": "Current session (5-hour)", "percent_used": 30, "resets_at": "2026-09-01T15:00:00Z", "severity": "normal"}
    ]


def test_fetch_includes_extra_usage_only_when_enabled():
    payload = {"limits": [], "extra_usage": {"is_enabled": True, "utilization": 12}}
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with _no_local_token(), mock.patch.object(
        claude_usage_client.requests, "get", return_value=_fake_response(json_payload=payload)
    ):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result["extra_usage_percent"] == 12


def test_fetch_returns_none_on_unauthorized():
    config = {"claude_usage": {"enabled": True, "access_token": "expired-tok"}}
    with _no_local_token(), mock.patch.object(
        claude_usage_client.requests, "get", return_value=_fake_response(status_code=401)
    ):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result is None


def test_fetch_returns_none_on_rate_limit():
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with _no_local_token(), mock.patch.object(
        claude_usage_client.requests, "get", return_value=_fake_response(status_code=429)
    ):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result is None


def test_fetch_returns_none_on_network_error():
    config = {"claude_usage": {"enabled": True, "access_token": "tok"}}
    with (
        _no_local_token(),
        mock.patch.object(
            claude_usage_client.requests, "get", side_effect=claude_usage_client.requests.ConnectionError("boom")
        ),
    ):
        result = claude_usage_client.fetch_claude_usage(config)

    assert result is None


def test_fetch_prefers_local_machine_token_over_config_fallback():
    config = {"claude_usage": {"enabled": True, "access_token": "config-fallback-token"}}

    with (
        mock.patch.object(claude_usage_client, "_read_local_claude_code_access_token", return_value="local-token"),
        mock.patch.object(claude_usage_client.requests, "get") as fake_get,
    ):
        fake_get.return_value = _fake_response(json_payload={"limits": []})
        claude_usage_client.fetch_claude_usage(config)

    assert fake_get.call_args.kwargs["headers"]["Authorization"] == "Bearer local-token"


def test_fetch_falls_back_to_config_token_when_no_local_login():
    config = {"claude_usage": {"enabled": True, "access_token": "config-fallback-token"}}

    with _no_local_token(), mock.patch.object(claude_usage_client.requests, "get") as fake_get:
        fake_get.return_value = _fake_response(json_payload={"limits": []})
        claude_usage_client.fetch_claude_usage(config)

    assert fake_get.call_args.kwargs["headers"]["Authorization"] == "Bearer config-fallback-token"


# --- _read_local_claude_code_access_token -----------------------------------


def _fake_security_result(*, returncode=0, stdout=""):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_read_local_token_macos_reads_keychain():
    payload = '{"claudeAiOauth": {"accessToken": "keychain-token"}}'
    with (
        mock.patch.object(claude_usage_client.sys, "platform", "darwin"),
        mock.patch.object(
            claude_usage_client.subprocess, "run", return_value=_fake_security_result(stdout=payload)
        ),
    ):
        assert claude_usage_client._read_local_claude_code_access_token() == "keychain-token"


def test_read_local_token_macos_returns_none_when_keychain_entry_missing():
    with (
        mock.patch.object(claude_usage_client.sys, "platform", "darwin"),
        mock.patch.object(claude_usage_client.subprocess, "run", return_value=_fake_security_result(returncode=44)),
    ):
        assert claude_usage_client._read_local_claude_code_access_token() is None


def test_read_local_token_linux_reads_credentials_file(tmp_path):
    creds_path = tmp_path / ".credentials.json"
    creds_path.write_text('{"claudeAiOauth": {"accessToken": "linux-token"}}', encoding="utf-8")

    with (
        mock.patch.object(claude_usage_client.sys, "platform", "linux"),
        mock.patch.object(claude_usage_client, "CLAUDE_CODE_LINUX_CREDENTIALS_PATH", creds_path),
    ):
        assert claude_usage_client._read_local_claude_code_access_token() == "linux-token"


def test_read_local_token_linux_returns_none_when_file_missing(tmp_path):
    with (
        mock.patch.object(claude_usage_client.sys, "platform", "linux"),
        mock.patch.object(
            claude_usage_client, "CLAUDE_CODE_LINUX_CREDENTIALS_PATH", tmp_path / "missing.json"
        ),
    ):
        assert claude_usage_client._read_local_claude_code_access_token() is None


def test_read_local_token_returns_none_on_malformed_json(tmp_path):
    creds_path = tmp_path / ".credentials.json"
    creds_path.write_text("not valid json", encoding="utf-8")

    with (
        mock.patch.object(claude_usage_client.sys, "platform", "linux"),
        mock.patch.object(claude_usage_client, "CLAUDE_CODE_LINUX_CREDENTIALS_PATH", creds_path),
    ):
        assert claude_usage_client._read_local_claude_code_access_token() is None
