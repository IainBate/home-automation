#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Resideo OAuth2 One-Time Setup (interactive CLI).

Resideo's thermostat API uses the OAuth2 authorization_code flow, which
needs one interactive browser consent step to get the first refresh token -
this cannot be scripted headlessly (see src/api_clients/resideo_client.py's
module docstring for the refresh-token rotation/expiry background). Run this
once per machine (or whenever a refresh token has gone stale and needs
replacing - "invalid_grant" errors in scripts/dashboard_server.py's logs
mean it's time to re-run this):

    python3 scripts/resideo_oauth_setup.py

Before running, at developer.honeywellhome.com, register a redirect URI
matching this script's callback (default http://localhost:8756/callback -
override with --port if that port is taken), and put client_id/client_secret
in config.yaml's resideo section / secrets.yaml (see config.yaml's resideo
comments) - this script needs both already present.

What it does:
    1. Opens your browser to Resideo's consent page.
    2. Runs a local web server just long enough to catch the redirect back
       (with an authorization code) - nothing here is exposed beyond your
       own machine.
    3. Exchanges that code for a refresh token, and saves it to
       config/resideo_token_state.json so the dashboard can use it
       immediately. Also prints it so you can copy it into secrets.yaml's
       resideo.refresh_token as a recovery copy (see config.yaml comments -
       this project keeps credentials there, not in config.yaml).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import base64
import logging
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from hotwater_automation_core import get_config_path

from src.api_clients.resideo_client import DEFAULT_TOKEN_URL
from src.config_manager.config_manager import load_static_config
from src.utils.paths import get_resideo_token_state_path
from src.utils.state_store import locked_json_state

logger = logging.getLogger(__name__)

DEFAULT_AUTHORIZE_URL = "https://api.honeywellhome.com/oauth2/authorize"
DEFAULT_CALLBACK_PORT = 8756


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the "?code=..." query param from Resideo's redirect, then stops the server."""

    authorization_code: str | None = None

    def do_GET(self) -> None:  # noqa: N802  # BaseHTTPRequestHandler's required method name
        query = parse_qs(urlparse(self.path).query)
        _CallbackHandler.authorization_code = (query.get("code") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        message = (
            "Authorization received - you can close this tab."
            if _CallbackHandler.authorization_code
            else "No authorization code received - check the terminal for details."
        )
        self.wfile.write(f"<html><body>{message}</body></html>".encode())

    def log_message(self, *_args: object) -> None:
        """Silence BaseHTTPRequestHandler's default per-request stderr logging."""


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT, help="Local callback port")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser


def run(config: dict, *, port: int) -> int:
    """Run the interactive OAuth flow. Returns 0 on success, 1 on failure."""
    resideo_config = config.get("resideo", {})
    client_id = resideo_config.get("client_id")
    client_secret = resideo_config.get("client_secret")
    if not client_id or not client_secret:
        print("resideo.client_id/client_secret must be set first - see config.yaml's resideo section")
        return 1

    redirect_uri = f"http://localhost:{port}/callback"
    authorize_url = resideo_config.get("authorize_url", DEFAULT_AUTHORIZE_URL)
    token_url = resideo_config.get("token_url", DEFAULT_TOKEN_URL)

    full_authorize_url = (
        f"{authorize_url}?{urlencode({'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri})}"
    )
    print(f"Redirect URI (must be registered exactly at developer.honeywellhome.com): {redirect_uri}")
    print(f"Opening browser to: {full_authorize_url}")
    webbrowser.open(full_authorize_url)

    server = HTTPServer(("localhost", port), _CallbackHandler)
    print(f"Waiting for the browser redirect on port {port}...")
    server.handle_request()
    server.server_close()

    code = _CallbackHandler.authorization_code
    if not code:
        print("No authorization code received - check that the redirect URI is registered exactly as shown above.")
        return 1

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
        token_url,
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        timeout=30,
    )
    if not response.ok:
        print(f"Token exchange failed: HTTP {response.status_code} - {response.text}")
        return 1

    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        print(f"Token exchange response had no refresh_token: {response.json()}")
        return 1

    with locked_json_state(get_resideo_token_state_path()) as state:
        state["refresh_token"] = refresh_token

    print("\nSuccess. The dashboard can use Resideo immediately (saved to config/resideo_token_state.json).")
    print(f"As a recovery copy, also paste this into secrets.yaml's resideo.refresh_token:\n\n  {refresh_token}\n")
    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s - %(levelname)s - %(message)s")

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, port=args.port))


if __name__ == "__main__":
    main()
