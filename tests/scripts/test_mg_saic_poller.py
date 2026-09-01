"""Tests for mg_saic_poller.py's cache-write and stale-on-failure behavior."""

from __future__ import annotations

import json
from unittest import mock

import mg_saic_poller as poller


def test_run_returns_1_when_disabled(capsys):
    exit_code = poller.run({"mg_saic": {"enabled": False}}, quiet=False)

    assert exit_code == 1
    assert "disabled" in capsys.readouterr().out


def test_run_writes_cache_on_success(tmp_path):
    status_path = tmp_path / "mg_saic_status.json"
    status = {"vehicle_name": "MG ZS", "battery_percent": 62.5, "range_km": 210.0, "is_charging": True, "is_parked": True}

    with (
        mock.patch.object(poller, "get_mg_saic_status_path", lambda: str(status_path)),
        mock.patch.object(poller, "fetch_saic_status", return_value=status),
    ):
        exit_code = poller.run({"mg_saic": {"enabled": True}}, quiet=True)

    assert exit_code == 0
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["battery_percent"] == 62.5
    assert "fetched_at" in saved


def test_run_leaves_previous_cache_in_place_on_failure(tmp_path):
    status_path = tmp_path / "mg_saic_status.json"
    status_path.write_text(json.dumps({"battery_percent": 40.0, "fetched_at": "old"}), encoding="utf-8")

    with (
        mock.patch.object(poller, "get_mg_saic_status_path", lambda: str(status_path)),
        mock.patch.object(poller, "fetch_saic_status", return_value=None),
    ):
        exit_code = poller.run({"mg_saic": {"enabled": True}}, quiet=True)

    assert exit_code == 1
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["fetched_at"] == "old"
