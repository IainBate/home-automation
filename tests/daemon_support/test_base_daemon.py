"""Unit tests for src/daemon_support/base_daemon.py's scheduling logic.

Drives TwoTierPollingDaemon._run_one_tick() directly (rather than run(),
which loops with real time.sleep() and installs signal handlers) so the
scheduling behaviour - a check fires immediately on first tick, then again
only once its own interval has elapsed, independent of other checks, and
gate-able as a whole via should_run_checks_this_tick() - can be pinned down
deterministically and fast.
"""

from __future__ import annotations

import logging

from src.daemon_support.base_daemon import TwoTierPollingDaemon, setup_rotating_logger


class _NoopConfigDaemon(TwoTierPollingDaemon):
    """A daemon with trivial config hooks, for tests that only care about scheduling."""

    def __init__(self) -> None:
        super().__init__()
        self.reload_calls = 0

    def load_config(self) -> None:
        pass

    def reload_config(self) -> None:
        self.reload_calls += 1


class _GatedDaemon(_NoopConfigDaemon):
    def __init__(self) -> None:
        super().__init__()
        self.gate_open = False

    def should_run_checks_this_tick(self) -> bool:
        return self.gate_open


def test_check_runs_on_first_tick_regardless_of_interval():
    daemon = _NoopConfigDaemon()
    calls = []
    daemon.register_check("c", lambda: calls.append(1), lambda: 10_000.0)

    daemon._run_one_tick()

    assert calls == [1]


def test_check_does_not_rerun_before_its_interval_elapses():
    daemon = _NoopConfigDaemon()
    calls = []
    daemon.register_check("c", lambda: calls.append(1), lambda: 10_000.0)

    daemon._run_one_tick()
    daemon._run_one_tick()
    daemon._run_one_tick()

    assert calls == [1]


def test_check_reruns_once_its_interval_elapses():
    daemon = _NoopConfigDaemon()
    calls = []
    daemon.register_check("c", lambda: calls.append(1), lambda: 0.0)

    daemon._run_one_tick()
    daemon._run_one_tick()

    assert calls == [1, 1]


def test_two_checks_are_scheduled_independently():
    daemon = _NoopConfigDaemon()
    fast_calls = []
    slow_calls = []
    daemon.register_check("fast", lambda: fast_calls.append(1), lambda: 0.0)
    daemon.register_check("slow", lambda: slow_calls.append(1), lambda: 10_000.0)

    daemon._run_one_tick()
    daemon._run_one_tick()
    daemon._run_one_tick()

    assert fast_calls == [1, 1, 1]
    assert slow_calls == [1]


def test_reload_config_runs_every_tick_even_with_no_due_checks():
    daemon = _NoopConfigDaemon()
    daemon.register_check("c", lambda: None, lambda: 10_000.0)

    daemon._run_one_tick()
    daemon._run_one_tick()

    assert daemon.reload_calls == 2


def test_gating_blocks_all_checks_and_does_not_advance_their_due_timer():
    daemon = _GatedDaemon()
    calls = []
    daemon.register_check("c", lambda: calls.append(1), lambda: 0.0)

    daemon.gate_open = False
    daemon._run_one_tick()
    daemon._run_one_tick()
    assert calls == []

    # Reopening the gate should run immediately - a skipped tick under the
    # gate must not have consumed the check's "due" window.
    daemon.gate_open = True
    daemon._run_one_tick()
    assert calls == [1]


def test_gating_still_reloads_config_even_when_checks_are_skipped():
    daemon = _GatedDaemon()
    daemon.gate_open = False

    daemon._run_one_tick()

    assert daemon.reload_calls == 1


def test_load_config_failure_propagates_from_run():
    class FailingDaemon(_NoopConfigDaemon):
        def load_config(self) -> None:
            raise ValueError("bad config")

    daemon = FailingDaemon()
    try:
        daemon.run()
    except ValueError as exc:
        assert str(exc) == "bad config"
    else:
        raise AssertionError("expected load_config's exception to propagate")


def test_shutdown_signal_handler_sets_flag():
    daemon = _NoopConfigDaemon()
    assert daemon.shutdown_requested is False
    daemon._handle_shutdown(15, None)
    assert daemon.shutdown_requested is True


def _reset_root_logger():
    """setup_rotating_logger() attaches handlers to the real root logger -
    clear them so a test doesn't leak handlers into the rest of the suite
    (same convention as tests/utils/test_logging_setup.py)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_setup_rotating_logger_captures_other_modules_warnings_via_root(tmp_path, monkeypatch):
    """A completely unrelated logger (standing in for src.api_clients._modbus_reader,
    which shares no ancestor with "battery_mode_daemon") must still reach the
    daemon's log file at WARNING+ - this is the blind spot found via the
    dashboard's health check missing real Modbus errors."""
    monkeypatch.chdir(tmp_path)
    _reset_root_logger()
    try:
        setup_rotating_logger("battery_mode_daemon_test", "test_daemon.log", level=logging.INFO)

        other_logger = logging.getLogger("src.api_clients._modbus_reader_test")
        other_logger.warning("Error reading work mode from 192.168.68.105")

        log_contents = (tmp_path / "logs" / "test_daemon.log").read_text(encoding="utf-8")
        assert "Error reading work mode from 192.168.68.105" in log_contents
        assert "src.api_clients._modbus_reader_test" in log_contents
    finally:
        _reset_root_logger()


def test_setup_rotating_logger_does_not_double_log_its_own_messages(tmp_path, monkeypatch):
    """The named logger gets no handlers of its own - only root's, reached via
    propagation - so its own records must appear exactly once."""
    monkeypatch.chdir(tmp_path)
    _reset_root_logger()
    try:
        logger = setup_rotating_logger("battery_mode_daemon_test2", "test_daemon2.log", level=logging.INFO)
        logger.info("Daemon started")

        log_contents = (tmp_path / "logs" / "test_daemon2.log").read_text(encoding="utf-8")
        assert log_contents.count("Daemon started") == 1
    finally:
        _reset_root_logger()


def test_setup_rotating_logger_does_not_pass_third_party_debug_chatter(tmp_path, monkeypatch):
    """Root's floor stays at WARNING regardless of the daemon's own (more
    verbose) level, so an unrelated module's routine DEBUG/INFO output isn't
    suddenly written to the daemon's log - only its own WARNING+ from
    setup_rotating_logger()'s explicit level is unaffected by this floor."""
    monkeypatch.chdir(tmp_path)
    _reset_root_logger()
    try:
        setup_rotating_logger("battery_mode_daemon_test3", "test_daemon3.log", level=logging.DEBUG)

        other_logger = logging.getLogger("src.api_clients._some_chatty_module_test")
        other_logger.info("Routine status check")

        log_contents = (tmp_path / "logs" / "test_daemon3.log").read_text(encoding="utf-8")
        assert "Routine status check" not in log_contents
    finally:
        _reset_root_logger()
