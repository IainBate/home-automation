"""Tests for get_ashp_config_error() in config_manager.py."""

from __future__ import annotations

from src.config_manager.config_manager import get_ashp_config_error


def test_error_when_resideo_disabled():
    error = get_ashp_config_error({"ashp": {"control_backend": "t6r"}, "resideo": {"enabled": False}})
    assert error is not None
    assert "resideo" in error


def test_none_when_resideo_enabled():
    error = get_ashp_config_error({"ashp": {"control_backend": "t6r"}, "resideo": {"enabled": True}})
    assert error is None


def test_defaults_to_t6r_backend_when_unspecified():
    error = get_ashp_config_error({"resideo": {"enabled": True}})
    assert error is None


def test_error_for_unimplemented_backend():
    error = get_ashp_config_error({"ashp": {"control_backend": "melcloud"}})
    assert error is not None
    assert "melcloud" in error
