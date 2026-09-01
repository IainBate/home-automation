"""Unit tests for src/daemon_support/base_daemon.py's scheduling logic.

Drives TwoTierPollingDaemon._run_one_tick() directly (rather than run(),
which loops with real time.sleep() and installs signal handlers) so the
scheduling behaviour - a check fires immediately on first tick, then again
only once its own interval has elapsed, independent of other checks, and
gate-able as a whole via should_run_checks_this_tick() - can be pinned down
deterministically and fast.
"""

from __future__ import annotations

from src.daemon_support.base_daemon import TwoTierPollingDaemon


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
