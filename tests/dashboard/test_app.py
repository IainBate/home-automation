"""Tests for app.py's Flask routes."""

from __future__ import annotations

from src.dashboard.app import create_app


class _FakePoller:
    def __init__(self, snapshot=None, age_seconds=None):
        self._snapshot = snapshot
        self._age_seconds = age_seconds

    def latest(self):
        return self._snapshot, self._age_seconds


def test_index_serves_html():
    app = create_app(_FakePoller())
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"Home Automation" in response.data


def test_api_status_returns_503_before_first_poll():
    app = create_app(_FakePoller(snapshot=None))
    client = app.test_client()

    response = client.get("/api/status")

    assert response.status_code == 503
    assert response.get_json() == {"ready": False}


def test_api_status_returns_cached_snapshot():
    app = create_app(_FakePoller(snapshot={"solar_battery": {"available": True}}, age_seconds=12.5))
    client = app.test_client()

    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ready"] is True
    assert body["poll_age_seconds"] == 12.5
    assert body["solar_battery"] == {"available": True}
