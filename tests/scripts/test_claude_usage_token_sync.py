"""Tests for claude_usage_token_sync.py - SSH push logic, without a real SSH connection.

subprocess.run is mocked throughout: this must never actually shell out to
ssh or the real Keychain in tests.
"""

from __future__ import annotations

from unittest import mock

import claude_usage_token_sync as sync


def _fake_ssh_result(*, returncode=0, stderr=""):
    result = mock.Mock()
    result.returncode = returncode
    result.stderr = stderr
    return result


def test_push_token_returns_true_on_success():
    with mock.patch.object(sync.subprocess, "run", return_value=_fake_ssh_result()) as fake_run:
        success = sync.push_token("tok-123", host="homepi4", user="pi", remote_repo_path="/home/pi/home_automation")

    assert success is True
    call_args = fake_run.call_args
    assert call_args.kwargs["input"] == '{"access_token": "tok-123"}'
    ssh_argv = call_args.args[0]
    assert ssh_argv[0] == "ssh"
    assert "pi@homepi4" in ssh_argv


def test_push_token_returns_false_on_ssh_failure(capsys):
    with mock.patch.object(
        sync.subprocess, "run", return_value=_fake_ssh_result(returncode=255, stderr="Connection refused")
    ):
        success = sync.push_token("tok-123", host="homepi4", user="pi", remote_repo_path="/home/pi/home_automation")

    assert success is False
    assert "Connection refused" in capsys.readouterr().err


def test_main_exits_1_when_keychain_read_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["claude_usage_token_sync.py", "--host", "homepi4"]
    )

    def fake_read():
        msg = "No Keychain entry"
        raise sync.KeychainReadError(msg)

    with mock.patch.object(sync, "read_credentials_from_keychain", fake_read):
        try:
            sync.main()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")

    assert "No Keychain entry" in capsys.readouterr().out


def test_main_exits_1_when_push_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["claude_usage_token_sync.py", "--host", "homepi4"])

    with (
        mock.patch.object(sync, "read_credentials_from_keychain", return_value={"access_token": "tok"}),
        mock.patch.object(sync, "push_token", return_value=False),
    ):
        try:
            sync.main()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")
