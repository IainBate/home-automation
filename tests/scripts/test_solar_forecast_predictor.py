"""Tests for solar_forecast_predictor.py's model-loading error handling.

Regression test for a gap the code-review skill caught: joblib.load() was the
only fallible call in this file not wrapped in try/except, unlike every other
client added alongside it (resideo/saic/airstage/claude_usage all degrade
gracefully instead of crashing the cron job on a corrupt/incompatible file).
"""

from __future__ import annotations

from unittest import mock

import solar_forecast_predictor as predictor


def test_run_returns_1_when_model_file_is_corrupt(tmp_path, capsys):
    model_path = tmp_path / "solar_forecast_model.joblib"
    model_path.write_bytes(b"not a valid joblib file")

    config = {
        "solar_forecast": {"enabled": True},
        "location": {"latitude": 51.5, "longitude": -0.1, "default_timezone_str": "Europe/London"},
    }

    with mock.patch.object(predictor, "get_solar_forecast_model_path", lambda: str(model_path)):
        exit_code = predictor.run(config, quiet=False)

    assert exit_code == 1
    assert "re-run scripts/solar_forecast_trainer.py" in capsys.readouterr().out


def test_run_returns_1_when_no_model_file_exists(tmp_path):
    config = {
        "solar_forecast": {"enabled": True},
        "location": {"latitude": 51.5, "longitude": -0.1},
    }

    with mock.patch.object(predictor, "get_solar_forecast_model_path", lambda: str(tmp_path / "missing.joblib")):
        exit_code = predictor.run(config, quiet=True)

    assert exit_code == 1


def test_carry_forward_yesterday_forecast_captures_completed_days_today_kwh():
    """First run after midnight: the previous record's "today_kwh" was for what is now yesterday."""
    previous_record = {"for_date": "2026-01-01", "today_kwh": 12.3}

    result = predictor._carry_forward_yesterday_forecast(previous_record, today_str="2026-01-02", yesterday_str="2026-01-01")

    assert result == 12.3


def test_carry_forward_yesterday_forecast_reuses_already_captured_value():
    """Later runs the same day: "today_kwh" has already moved on, so reuse what was captured earlier."""
    previous_record = {"for_date": "2026-01-02", "today_kwh": 9.9, "yesterday_forecast_kwh": 12.3}

    result = predictor._carry_forward_yesterday_forecast(previous_record, today_str="2026-01-02", yesterday_str="2026-01-01")

    assert result == 12.3


def test_carry_forward_yesterday_forecast_returns_none_across_a_gap():
    """The predictor didn't run at all yesterday - no meaningful forecast to compare against."""
    previous_record = {"for_date": "2025-12-30", "today_kwh": 5.0}

    result = predictor._carry_forward_yesterday_forecast(previous_record, today_str="2026-01-02", yesterday_str="2026-01-01")

    assert result is None


def test_carry_forward_yesterday_forecast_returns_none_when_no_previous_record():
    assert predictor._carry_forward_yesterday_forecast({}, today_str="2026-01-02", yesterday_str="2026-01-01") is None
