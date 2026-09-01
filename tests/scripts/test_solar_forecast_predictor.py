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
