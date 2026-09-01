"""Tests for claude_usage_token_extract.py - Keychain parsing, without touching a real Keychain.

subprocess.run is mocked throughout: this must never shell out to the real
/usr/bin/security in tests, both because CI/other machines may not have a
matching Keychain entry and because it's a genuine credential read that
should only happen when the script is actually run by a person.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

import claude_usage_token_extract as extractor


def _fake_result(*, returncode=0, stdout=""):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_main_exits_cleanly_on_non_macos(capsys):
    with mock.patch.object(extractor.sys, "platform", "linux"), pytest.raises(SystemExit) as exc_info:
        extractor.main()

    assert exc_info.value.code == 1
    assert "macOS" in capsys.readouterr().out


def test_main_reports_missing_keychain_entry(capsys):
    with (
        mock.patch.object(extractor.sys, "platform", "darwin"),
        mock.patch.object(extractor.subprocess, "run", return_value=_fake_result(returncode=44, stdout="")),
        pytest.raises(SystemExit) as exc_info,
    ):
        extractor.main()

    assert exc_info.value.code == 1
    assert "No 'Claude Code-credentials' entry" in capsys.readouterr().out


def test_main_prints_access_token_snippet(capsys):
    payload = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-test-token", "subscriptionType": "max"}})
    with (
        mock.patch.object(extractor.sys, "platform", "darwin"),
        mock.patch.object(extractor.subprocess, "run", return_value=_fake_result(stdout=payload)),
    ):
        extractor.main()

    output = capsys.readouterr().out
    assert "sk-ant-test-token" in output
    assert "access_token:" in output


def test_main_reports_malformed_keychain_payload(capsys):
    with (
        mock.patch.object(extractor.sys, "platform", "darwin"),
        mock.patch.object(extractor.subprocess, "run", return_value=_fake_result(stdout="{}")),
        pytest.raises(SystemExit) as exc_info,
    ):
        extractor.main()

    assert exc_info.value.code == 1
    assert "didn't contain the expected fields" in capsys.readouterr().out
