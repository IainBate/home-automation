"""Tests for src/utils/emailer.py (msmtp-based email sending)."""

from __future__ import annotations

import subprocess
from unittest import mock

from src.utils import emailer


def _fake_result(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_send_email_disabled_returns_false_without_calling_msmtp():
    config = {"email": {"enabled": False, "to_address": "iain.bate@gmail.com"}}
    with mock.patch.object(emailer.subprocess, "run") as run:
        result = emailer.send_email(config, "subject", "body")
    assert result is False
    run.assert_not_called()


def test_send_email_missing_to_address_returns_false():
    config = {"email": {"enabled": True}}
    with mock.patch.object(emailer.subprocess, "run") as run:
        result = emailer.send_email(config, "subject", "body")
    assert result is False
    run.assert_not_called()


def test_send_email_success_invokes_msmtp_with_recipient():
    config = {"email": {"enabled": True, "to_address": "iain.bate@gmail.com"}}
    with mock.patch.object(emailer.subprocess, "run", return_value=_fake_result()) as run:
        result = emailer.send_email(config, "Test subject", "Test body")

    assert result is True
    args, kwargs = run.call_args
    command = args[0]
    assert command[0] == "msmtp"
    assert command[-1] == "iain.bate@gmail.com"
    assert "Test subject" in kwargs["input"]
    assert "Test body" in kwargs["input"]


def test_send_email_uses_custom_msmtp_account():
    config = {
        "email": {"enabled": True, "to_address": "iain.bate@gmail.com", "msmtp_account": "gmail"}
    }
    with mock.patch.object(emailer.subprocess, "run", return_value=_fake_result()) as run:
        emailer.send_email(config, "subject", "body")

    command = run.call_args[0][0]
    assert "-a" in command
    assert command[command.index("-a") + 1] == "gmail"


def test_send_email_nonzero_exit_returns_false():
    config = {"email": {"enabled": True, "to_address": "iain.bate@gmail.com"}}
    with mock.patch.object(
        emailer.subprocess, "run", return_value=_fake_result(returncode=1, stderr="boom")
    ):
        result = emailer.send_email(config, "subject", "body")
    assert result is False


def test_send_email_missing_binary_returns_false_not_raises():
    config = {"email": {"enabled": True, "to_address": "iain.bate@gmail.com"}}
    with mock.patch.object(emailer.subprocess, "run", side_effect=FileNotFoundError()):
        result = emailer.send_email(config, "subject", "body")
    assert result is False


def test_send_email_unexpected_exception_returns_false_not_raises():
    """Circuit Breaker: sending the alert must never crash the caller."""
    config = {"email": {"enabled": True, "to_address": "iain.bate@gmail.com"}}
    with mock.patch.object(emailer.subprocess, "run", side_effect=RuntimeError("boom")):
        result = emailer.send_email(config, "subject", "body")
    assert result is False
