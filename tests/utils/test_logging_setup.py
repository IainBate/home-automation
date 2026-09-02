"""Tests for src/utils/logging_setup.py."""

from __future__ import annotations

import logging
import logging.handlers

from src.utils import logging_setup


def _reset_root_logger():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_not_quiet_attaches_stream_handler_to_root(monkeypatch):
    _reset_root_logger()
    try:
        logging_setup.configure_cron_safe_logging(
            level=logging.WARNING, quiet=False, log_filename="x.log"
        )
        root = logging.getLogger()
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        assert not any(
            isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root.handlers
        )
    finally:
        _reset_root_logger()


def test_quiet_attaches_file_handler_not_stream_handler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_root_logger()
    try:
        logging_setup.configure_cron_safe_logging(
            level=logging.WARNING, quiet=True, log_filename="mypoller.log"
        )
        root = logging.getLogger()
        assert any(
            isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root.handlers
        )
        assert not any(
            type(h) is logging.StreamHandler for h in root.handlers  # noqa: E721 - exact type, not subclass
        )
        assert (tmp_path / "logs").is_dir()
    finally:
        _reset_root_logger()


def test_quiet_logging_writes_warning_to_file_not_stdout(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_root_logger()
    try:
        logging_setup.configure_cron_safe_logging(
            level=logging.WARNING, quiet=True, log_filename="mypoller.log"
        )
        logging.getLogger("some.module").warning("token expired")

        captured = capsys.readouterr()
        assert "token expired" not in captured.err
        assert "token expired" not in captured.out
        assert "token expired" in (tmp_path / "logs" / "mypoller.log").read_text()
    finally:
        _reset_root_logger()
