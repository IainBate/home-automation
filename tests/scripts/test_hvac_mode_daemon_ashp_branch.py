"""Tests for hvac_mode_daemon.py's ASHP branch point in _run_hvac_target_update_cycle
and the should_run_checks_this_tick config-error gate extension.

(tests/scripts/test_hvac_mode_daemon_scheduling.py once covered this daemon's
general scheduling behavior but was lost the same way the daemon source
itself was - see docs/hvac_thermostat_automation_plan.md step 5's
2026-09-09 correction. Not recovered - not worth the effort for a test
file when fresh, current tests can be written directly instead.)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import hvac_mode_daemon as daemon_module  # noqa: E402


def _make_daemon(config):
    with mock.patch.object(daemon_module, "setup_rotating_logger", lambda *a, **k: mock.Mock()):
        d = daemon_module.HvacModeDaemon(config_path="/x.yaml")
    d.config = config
    d.logger = mock.Mock()
    return d


def test_ashp_disabled_calls_run_hvac_decision_check_directly():
    config = {"hvac_automation": {}, "ashp": {"enabled": False}}
    d = _make_daemon(config)

    with mock.patch.object(daemon_module, "run_hvac_decision_check") as fake_hvac, mock.patch.object(
        daemon_module, "run_ashp_decision_check"
    ) as fake_ashp:
        d._run_hvac_target_update_cycle(config["hvac_automation"])

    fake_hvac.assert_called_once()
    fake_ashp.assert_not_called()


def test_ashp_enabled_calls_run_ashp_decision_check_instead():
    config = {"hvac_automation": {}, "ashp": {"enabled": True}}
    d = _make_daemon(config)

    with mock.patch.object(daemon_module, "run_hvac_decision_check") as fake_hvac, mock.patch.object(
        daemon_module, "run_ashp_decision_check"
    ) as fake_ashp:
        d._run_hvac_target_update_cycle(config["hvac_automation"])

    fake_ashp.assert_called_once()
    fake_hvac.assert_not_called()


def test_ashp_config_absent_defaults_to_disabled():
    """No "ashp" key at all in config.yaml (e.g. an old config predating this
    feature) must behave exactly as ashp.enabled: false, not error."""
    config = {"hvac_automation": {}}
    d = _make_daemon(config)

    with mock.patch.object(daemon_module, "run_hvac_decision_check") as fake_hvac, mock.patch.object(
        daemon_module, "run_ashp_decision_check"
    ) as fake_ashp:
        d._run_hvac_target_update_cycle(config["hvac_automation"])

    fake_hvac.assert_called_once()
    fake_ashp.assert_not_called()


def test_should_run_checks_this_tick_gates_on_ashp_config_error_only_when_enabled():
    config = {
        "hvac_automation": {"enabled": True},
        "ashp": {"enabled": True},
    }
    d = _make_daemon(config)

    with mock.patch.object(daemon_module, "get_hvac_automation_config_error", return_value=None), \
         mock.patch.object(daemon_module, "get_ashp_config_error", return_value="ashp misconfigured"):
        assert d.should_run_checks_this_tick() is False


def test_should_run_checks_this_tick_ignores_ashp_error_when_ashp_disabled():
    config = {
        "hvac_automation": {"enabled": True},
        "ashp": {"enabled": False},
    }
    d = _make_daemon(config)

    with mock.patch.object(daemon_module, "get_hvac_automation_config_error", return_value=None), \
         mock.patch.object(daemon_module, "get_ashp_config_error", return_value="ashp misconfigured") as fake_ashp_err:
        result = d.should_run_checks_this_tick()

    assert result is True
    fake_ashp_err.assert_not_called()
