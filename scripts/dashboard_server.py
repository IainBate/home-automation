#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Home Automation Status Dashboard - read-only web page for phones on the LAN.

Runs as its own standalone process, independent of battery_mode_daemon.py and
hotwater_mode_daemon.py: it only ever reads (SolaX Modbus, Ohme, MELCloud),
never calls a mode-change/force-heat/charger-control function, and never
touches either daemon's state files. Safe to start, stop, or crash without
affecting either automation.

A background thread refreshes a cached status snapshot every
web_interface.poll_interval_seconds; Flask requests just read that cache, so
page loads never block on a slow inverter/cloud call and load doesn't scale
with how many phones have the page open.

Usage:
    python3 scripts/dashboard_server.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from typing import Any

from hotwater_automation_core import get_config_path

from src.config_manager.config_manager import load_static_config
from src.dashboard.app import create_app
from src.dashboard.poller import StatusPoller
from src.dashboard.status_collector import collect_status
from src.daemon_support.base_daemon import setup_rotating_logger

DEFAULT_POLL_INTERVAL_SECONDS = 45


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (defaults to the project root's config.yaml)",
    )
    return parser


def main() -> None:
    """Load config, start the background poller, and serve the dashboard until interrupted."""
    args = _create_argument_parser().parse_args()
    config_path = args.config or get_config_path()

    logger = setup_rotating_logger("dashboard_server", "dashboard_server.log", level=logging.INFO)

    config: dict[str, Any] | None = load_static_config(config_path)
    if config is None:
        logger.error("Failed to load config from %s - see logs above for details", config_path)
        sys.exit(1)

    web_config = config.get("web_interface", {})
    if not web_config.get("enabled", False):
        logger.info("Dashboard disabled (web_interface.enabled: false in config.yaml) - exiting")
        sys.exit(0)

    poll_interval_seconds = web_config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    poller = StatusPoller(
        lambda: collect_status(config, config_path), poll_interval_seconds=poll_interval_seconds
    )

    logger.info("Starting dashboard status poller (interval: %ss)...", poll_interval_seconds)
    poller.start()

    app = create_app(poller)
    host = web_config["host"]
    port = web_config["port"]
    debug_mode = web_config.get("debug_mode", False)

    logger.info("Dashboard serving on http://%s:%d (debug=%s)", host, port, debug_mode)
    try:
        app.run(host=host, port=port, debug=debug_mode, use_reloader=False, threaded=True)
    finally:
        logger.info("Dashboard shutting down...")
        poller.stop()


if __name__ == "__main__":
    main()
