"""Flask app for the read-only status dashboard.

Every route only reads from the StatusPoller's cache - nothing here ever
calls a hardware/API write function (mode changes, force-heat, charger
control), and nothing here touches battery_mode_daemon.py or
hotwater_mode_daemon.py's own state files. This process is safe to run
alongside both of those daemons at all times.
"""

from __future__ import annotations

from flask import Flask, Response, jsonify

from src.dashboard.poller import StatusPoller
from src.dashboard.static_page import DASHBOARD_HTML


def create_app(poller: StatusPoller) -> Flask:
    """Build the Flask app, wiring routes to the given (already-started) poller."""
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/api/status")
    def api_status() -> Response:
        snapshot, age_seconds = poller.latest()
        if snapshot is None:
            return jsonify({"ready": False}), 503
        return jsonify({"ready": True, "poll_age_seconds": age_seconds, **snapshot})

    return app
