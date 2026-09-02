"""Tests for solax_cloud_client.py's HTTP error handling.

Regression test for a bug found diagnosing a live "token invalid!" failure
(2026-09-02): the real API's failure shape is a top-level
{"exception": "...", "code": ...} with no "result" key at all, but the old
code only ever looked at result.msg, so every failure logged a useless
"Unknown error" instead of the API's actual reason.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from src.api_clients import solax_cloud_client


def _fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_solax_cloud_get_daily_yield_logs_real_api_exception_message(caplog):
    config = {"solaX_cloud_api": {"token_id": "some-token", "master_wifisn": "SR2NZD2S3B"}}
    payload = {"exception": "token invalid!", "code": 103, "tokenId": "some-token", "success": False}

    with mock.patch.object(solax_cloud_client.requests, "post", return_value=_fake_response(payload)):
        result = solax_cloud_client.solax_cloud_get_daily_yield(config, date(2026, 9, 1))

    assert result is None
    assert "token invalid!" in caplog.text
    assert "Unknown error" not in caplog.text


def test_solax_cloud_get_realtime_snapshot_parses_successful_response():
    config = {"solaX_cloud_api": {"token_id": "some-token", "master_wifisn": "SR2NZD2S3B"}}
    payload = {
        "success": True,
        "exception": "Query success!",
        "result": {},
        "uploadTime": "2026-09-02 21:37:53",
        "soc": 99.0,
        "batPower": -70.0,
        "feedinpower": -154.0,
        "powerdc1": 0.0,
        "powerdc2": 0.0,
        "powerdc3": None,
        "powerdc4": None,
    }

    with mock.patch.object(solax_cloud_client.requests, "get", return_value=_fake_response(payload)):
        snapshot = solax_cloud_client.solax_cloud_get_realtime_snapshot(config)

    assert snapshot == {
        "timestamp": "2026-09-02 21:37:53",
        "pv_power_kw": 0.0,
        "battery_power_kw": -0.07,
        "grid_power_kw": -0.154,
        "soc_percent": 99,
    }


def test_solax_cloud_get_realtime_snapshot_returns_none_on_token_invalid(caplog):
    config = {"solaX_cloud_api": {"token_id": "bad-token", "master_wifisn": "SR2NZD2S3B"}}
    payload = {"exception": "token invalid!", "code": 103, "success": False}

    with mock.patch.object(solax_cloud_client.requests, "get", return_value=_fake_response(payload)):
        snapshot = solax_cloud_client.solax_cloud_get_realtime_snapshot(config)

    assert snapshot is None
    assert "token invalid!" in caplog.text


def test_solax_cloud_get_realtime_snapshot_returns_none_without_credentials():
    assert solax_cloud_client.solax_cloud_get_realtime_snapshot({"solaX_cloud_api": {}}) is None


def test_merge_realtime_snapshot_appends_to_empty_record():
    snapshot = {"timestamp": "2026-09-02 21:40:00", "pv_power_kw": 0.0, "soc_percent": 99}

    updated = solax_cloud_client.merge_realtime_snapshot({}, snapshot)

    assert updated["data"] == [snapshot]
    assert updated["meta"]["data_points"] == 1
    assert updated["meta"]["date_range"] == {"start": "2026-09-02", "end": "2026-09-02"}


def test_merge_realtime_snapshot_appends_to_existing_data():
    existing = {
        "meta": {"data_points": 1, "date_range": {"start": "2026-09-01", "end": "2026-09-01"}},
        "data": [{"timestamp": "2026-09-01 12:00:00", "pv_power_kw": 2.0, "soc_percent": 80}],
    }
    snapshot = {"timestamp": "2026-09-02 08:00:00", "pv_power_kw": 1.0, "soc_percent": 85}

    updated = solax_cloud_client.merge_realtime_snapshot(existing, snapshot)

    assert [e["timestamp"] for e in updated["data"]] == ["2026-09-01 12:00:00", "2026-09-02 08:00:00"]
    assert updated["meta"]["data_points"] == 2
    assert updated["meta"]["date_range"] == {"start": "2026-09-01", "end": "2026-09-02"}


def test_merge_realtime_snapshot_is_a_noop_for_a_duplicate_timestamp():
    existing = {
        "meta": {"data_points": 1, "date_range": {"start": "2026-09-02", "end": "2026-09-02"}},
        "data": [{"timestamp": "2026-09-02 08:00:00", "pv_power_kw": 1.0, "soc_percent": 85}],
    }
    snapshot = {"timestamp": "2026-09-02 08:00:00", "pv_power_kw": 999.0, "soc_percent": 1}

    updated = solax_cloud_client.merge_realtime_snapshot(existing, snapshot)

    assert updated is existing
    assert updated["data"][0]["pv_power_kw"] == 1.0


def test_solax_cloud_get_daily_yield_parses_successful_response():
    config = {"solaX_cloud_api": {"token_id": "some-token", "master_wifisn": "SR2NZD2S3B"}}
    payload = {
        "success": 1,
        "result": {
            "powerDataList": [
                {
                    "dt": "2026-09-01 12:00:00",
                    "pvPower": 2000,
                    "batteryPower": -500,
                    "feedInPower": 1000,
                    "consumptionPower": 1500,
                    "soc": 80,
                }
            ]
        },
    }

    with mock.patch.object(solax_cloud_client.requests, "post", return_value=_fake_response(payload)):
        result = solax_cloud_client.solax_cloud_get_daily_yield(config, date(2026, 9, 1))

    assert result == [
        {
            "timestamp": "2026-09-01 12:00:00",
            "timestamp_utc": "2026-09-01T12:00:00+00:00",
            "datetime_obj": mock.ANY,
            "pv_power_kw": 2.0,
            "battery_power_kw": -0.5,
            "grid_power_kw": 1.0,
            "load_power_kw": 1.5,
            "soc_percent": 80,
        }
    ]
