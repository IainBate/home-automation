"""Tests for get_hotwater_automation_config_error() in hotwater_automation_core.py.

Regression coverage for a code-review finding: this used to independently
re-derive the "melcloud must be enabled and configured" condition already
checked (with different wording) by config_manager.validate_business_rules() -
now both delegate to config_manager.get_hotwater_melcloud_config_error().
"""

from __future__ import annotations

import hotwater_automation_core as core


def test_error_when_melcloud_disabled():
    error = core.get_hotwater_automation_config_error({"melcloud": {"enabled": False}})
    assert error is not None


def test_error_when_credentials_missing():
    error = core.get_hotwater_automation_config_error({"melcloud": {"enabled": True}})
    assert error is not None


def test_none_when_fully_configured():
    error = core.get_hotwater_automation_config_error(
        {"melcloud": {"enabled": True, "email": "a@b.com", "password": "x"}}
    )
    assert error is None


def test_delegates_to_the_shared_config_manager_check(monkeypatch):
    calls = []

    def spy(config):
        calls.append(config)
        return "spied"

    monkeypatch.setattr(core, "get_hotwater_melcloud_config_error", spy)

    result = core.get_hotwater_automation_config_error({"some": "config"})

    assert result == "spied"
    assert calls == [{"some": "config"}]
