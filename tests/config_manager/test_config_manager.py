"""Unit tests for src/config_manager/config_manager.py.

validate_config_schema and validate_business_rules are pure functions over a
config dict - no file I/O needed. _deep_merge is pure too. load_static_config
and _load_secrets_overlay do real (but tmp-dir-isolated) file I/O, exercised
against the real project's config.yaml as a known-valid base rather than a
hand-built minimal schema-valid dict (this schema has ~10 required top-level
sections - reusing the real file is far less brittle than reconstructing one).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from src.config_manager.config_manager import (
    validate_business_rule_errors,
    _deep_merge,
    _find_unknown_overlay_keys,
    _load_secrets_overlay,
    get_hotwater_melcloud_config_error,
    load_static_config,
    validate_business_rules,
    validate_config_schema,
)

PROJECT_ROOT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def _real_config() -> dict:
    with PROJECT_ROOT_CONFIG.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- validate_config_schema --------------------------------------------------


def test_schema_validation_passes_for_the_real_config():
    assert validate_config_schema(_real_config()) == []


def test_schema_validation_reports_missing_required_top_level_section():
    config = _real_config()
    del config["battery_system"]

    errors = validate_config_schema(config)

    assert len(errors) >= 1
    assert "battery_system" in errors[0]


def test_schema_validation_reports_wrong_type_with_hint():
    config = _real_config()
    config["battery_system"]["master_capacity_kwh"] = "not a number"

    errors = validate_config_schema(config)

    assert any("Check the data type" in e for e in errors)


# --- validate_business_rules --------------------------------------------------


def _valid_business_config(**overrides) -> dict:
    config = {
        "battery_system": {
            "master_capacity_kwh": 10.0,
            "slave_capacity_kwh": 10.0,
            "simulation": {"charge_efficiency_percent": 95, "discharge_efficiency_percent": 95},
        },
        "household_load": {
            "base_load_daytime_kw": 1.0,
            "base_load_nighttime_kw": 0.5,
            "daytime_start_hour": 7,
            "daytime_end_hour": 23,
        },
        "financial_costs": {
            "fixed_export_price_per_kwh": 0.15,
            "cheap_charge_threshold_per_kwh": 0.10,
            "high_price_reserve_threshold_per_kwh": 0.30,
        },
        "api_settings": {"timeout_seconds": 30, "calls_per_day": 8},
        "hotwater_automation": {"enabled": False},
        "melcloud": {"enabled": False},
    }
    for section, values in overrides.items():
        config.setdefault(section, {})
        config[section] = {**config.get(section, {}), **values}
    return config


def test_business_rules_valid_config_has_no_warnings():
    assert validate_business_rules(_valid_business_config()) == []


def test_business_rules_flags_capacity_mismatch():
    warnings = validate_business_rules(
        _valid_business_config(battery_system={"master_capacity_kwh": 10.0, "slave_capacity_kwh": 2.0})
    )
    assert any("capacities differ" in w for w in warnings)


def test_business_rules_flags_daytime_load_less_than_nighttime():
    warnings = validate_business_rules(
        _valid_business_config(
            household_load={"base_load_daytime_kw": 0.2, "base_load_nighttime_kw": 1.0}
        )
    )
    assert any("Daytime load is less than nighttime" in w for w in warnings)


def test_daytime_window_inversion_blocks_loading_rather_than_warning():
    """This one is a real contradiction, not an oddity - it now has to stop
    the config loading rather than log a warning nobody reads."""
    errors = validate_business_rule_errors(
        _valid_business_config(household_load={"daytime_start_hour": 23, "daytime_end_hour": 7})
    )
    assert any("must be before" in e for e in errors)


def test_a_sane_daytime_window_produces_no_blocking_errors():
    assert validate_business_rule_errors(
        _valid_business_config(household_load={"daytime_start_hour": 7, "daytime_end_hour": 23})
    ) == []


def test_business_rules_flags_cheap_threshold_above_high_threshold():
    warnings = validate_business_rules(
        _valid_business_config(
            financial_costs={
                "cheap_charge_threshold_per_kwh": 0.40,
                "high_price_reserve_threshold_per_kwh": 0.30,
            }
        )
    )
    assert any("less than high price reserve" in w for w in warnings)


def test_business_rules_flags_cheap_threshold_above_export_price():
    warnings = validate_business_rules(
        _valid_business_config(
            financial_costs={
                "fixed_export_price_per_kwh": 0.10,
                "cheap_charge_threshold_per_kwh": 0.20,
                "high_price_reserve_threshold_per_kwh": 0.30,
            }
        )
    )
    assert any("higher than export price" in w for w in warnings)


def test_business_rules_flags_low_battery_efficiency():
    warnings = validate_business_rules(
        _valid_business_config(
            battery_system={
                "master_capacity_kwh": 10.0,
                "slave_capacity_kwh": 10.0,
                "simulation": {"charge_efficiency_percent": 80, "discharge_efficiency_percent": 95},
            }
        )
    )
    assert any("efficiency below 85%" in w for w in warnings)


def test_business_rules_flags_short_api_timeout():
    warnings = validate_business_rules(_valid_business_config(api_settings={"timeout_seconds": 2}))
    assert any("timeout is very short" in w for w in warnings)


def test_business_rules_flags_high_calls_per_day():
    warnings = validate_business_rules(
        _valid_business_config(api_settings={"calls_per_day": 500})
    )
    assert any("Solcast API calls" in w for w in warnings)


def test_business_rules_flags_hotwater_enabled_without_melcloud():
    warnings = validate_business_rules(
        _valid_business_config(hotwater_automation={"enabled": True}, melcloud={"enabled": False})
    )
    assert any("melcloud.enabled is" in w for w in warnings)


def test_business_rules_flags_hotwater_enabled_melcloud_missing_credentials():
    warnings = validate_business_rules(
        _valid_business_config(
            hotwater_automation={"enabled": True}, melcloud={"enabled": True}
        )
    )
    assert any("email/password" in w for w in warnings)


def test_business_rules_hotwater_and_melcloud_both_fully_configured_is_clean():
    warnings = validate_business_rules(
        _valid_business_config(
            hotwater_automation={"enabled": True},
            melcloud={"enabled": True, "email": "a@b.com", "password": "x"},
        )
    )
    assert warnings == []


# --- get_hotwater_melcloud_config_error --------------------------------------
# Regression coverage for a code-review finding: this exact condition used to
# be duplicated (independently worded) in both validate_business_rules() and
# scripts/hotwater_automation_core.py's get_hotwater_automation_config_error() -
# now both delegate to this one shared function.


def test_hotwater_melcloud_error_when_melcloud_disabled():
    error = get_hotwater_melcloud_config_error({"melcloud": {"enabled": False}})
    assert error is not None
    assert "melcloud.enabled is false" in error


def test_hotwater_melcloud_error_when_credentials_missing():
    error = get_hotwater_melcloud_config_error({"melcloud": {"enabled": True}})
    assert error is not None
    assert "email/password" in error


def test_hotwater_melcloud_error_none_when_fully_configured():
    error = get_hotwater_melcloud_config_error(
        {"melcloud": {"enabled": True, "email": "a@b.com", "password": "x"}}
    )
    assert error is None


def test_validate_business_rules_uses_the_shared_hotwater_melcloud_check(monkeypatch):
    """Confirm validate_business_rules() actually delegates rather than having
    its own independent copy of the condition.
    """
    import src.config_manager.config_manager as config_manager_module

    calls = []

    def spy(config_data):
        calls.append(config_data)
        return "spied error message"

    monkeypatch.setattr(config_manager_module, "get_hotwater_melcloud_config_error", spy)

    warnings = validate_business_rules(_valid_business_config(hotwater_automation={"enabled": True}))

    assert len(calls) == 1
    assert any("spied error message" in w for w in warnings)


# --- _deep_merge --------------------------------------------------------------


def test_deep_merge_overlay_wins_on_scalar_conflict():
    assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_deep_merge_recurses_into_nested_dicts():
    base = {"melcloud": {"enabled": False, "email": "placeholder"}}
    overlay = {"melcloud": {"email": "real@example.com"}}
    merged = _deep_merge(base, overlay)
    assert merged == {"melcloud": {"enabled": False, "email": "real@example.com"}}


def test_deep_merge_overlay_dict_replaces_non_dict_base_value():
    merged = _deep_merge({"a": 1}, {"a": {"b": 2}})
    assert merged == {"a": {"b": 2}}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    overlay = {"a": {"c": 2}}
    _deep_merge(base, overlay)
    assert base == {"a": {"b": 1}}
    assert overlay == {"a": {"c": 2}}


# --- _find_unknown_overlay_keys -----------------------------------------------


def test_find_unknown_overlay_keys_flags_unknown_top_level_section():
    unknown = _find_unknown_overlay_keys({"ohme": {"username": "x"}}, {"ohme_ev": {}})
    assert unknown == ["ohme"]


def test_find_unknown_overlay_keys_flags_unknown_nested_key():
    unknown = _find_unknown_overlay_keys(
        {"melcloud": {"emial": "x", "password": "y"}}, {"melcloud": {"email": "", "password": ""}}
    )
    assert unknown == ["melcloud.emial"]


def test_find_unknown_overlay_keys_no_false_positives_for_known_keys():
    unknown = _find_unknown_overlay_keys(
        {"melcloud": {"email": "x", "password": "y"}}, {"melcloud": {"email": "", "password": ""}}
    )
    assert unknown == []


def test_find_unknown_overlay_keys_handles_multiple_nesting_levels():
    unknown = _find_unknown_overlay_keys(
        {"melcloud": {"mode_change_retry": {"max_attemtps": 3}}},
        {"melcloud": {"mode_change_retry": {"max_attempts": 4}}},
    )
    assert unknown == ["melcloud.mode_change_retry.max_attemtps"]


# --- _load_secrets_overlay / load_static_config (tmp-dir file I/O) -----------


def test_load_secrets_overlay_missing_file_returns_empty_dict(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("a: 1", encoding="utf-8")
    assert _load_secrets_overlay(str(config_path)) == {}


def test_load_secrets_overlay_merges_real_credentials(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("melcloud:\n  enabled: false\n", encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text(
        "melcloud:\n  email: real@example.com\n  password: secret\n", encoding="utf-8"
    )

    overlay = _load_secrets_overlay(str(config_path))

    assert overlay == {"melcloud": {"email": "real@example.com", "password": "secret"}}


def test_load_secrets_overlay_non_dict_content_is_ignored(tmp_path, caplog):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("a: 1", encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        overlay = _load_secrets_overlay(str(config_path))

    assert overlay == {}


def test_load_static_config_warns_on_unknown_secrets_section(tmp_path, caplog):
    real_config = _real_config()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(real_config), encoding="utf-8")
    # "ohme" (typo for "ohme_ev") - a section name absent from config.yaml.
    (tmp_path / "secrets.yaml").write_text(
        "ohme:\n  username: typo@example.com\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING):
        result = load_static_config(str(config_path))

    assert result is not None  # the typo doesn't fail the load, just warns
    assert any("not found in" in message for message in caplog.messages)


def test_load_static_config_warns_on_unknown_nested_secrets_key(tmp_path, caplog):
    """Regression test: the typo-detection used to only diff top-level
    section names (set(secrets_overlay) - set(config)), so a typo'd *nested*
    key under an otherwise-valid section (e.g. "melcloud.emial" instead of
    "melcloud.email") went completely undetected - "melcloud" itself is a
    real section, so nothing was ever flagged.
    """
    real_config = _real_config()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(real_config), encoding="utf-8")
    # "melcloud" is a real section, but "emial" (typo for "email") is not a
    # real key within it.
    (tmp_path / "secrets.yaml").write_text(
        "melcloud:\n  emial: typo@example.com\n  password: real-password\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_static_config(str(config_path))

    assert result is not None  # the typo doesn't fail the load, just warns
    assert any("melcloud.emial" in message for message in caplog.messages)
    # The correctly-spelled sibling key must NOT be flagged.
    assert not any("melcloud.password" in message for message in caplog.messages)


def test_load_static_config_returns_none_for_missing_file(tmp_path):
    assert load_static_config(str(tmp_path / "does_not_exist.yaml")) is None


def test_load_static_config_computes_total_capacity(tmp_path):
    real_config = _real_config()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(real_config), encoding="utf-8")

    result = load_static_config(str(config_path))

    assert result is not None
    expected_total = (
        real_config["battery_system"]["master_capacity_kwh"]
        + real_config["battery_system"]["slave_capacity_kwh"]
    )
    assert result["battery_system"]["total_capacity_kwh"] == pytest.approx(expected_total)
