"""Tests for resideo_oauth_setup.py's local OAuth callback server and error handling.

The token-exchange network call itself is out of scope here (that's
resideo_client.refresh_access_token's own contract, tested in
test_resideo_client.py) - this file covers the parts unique to the
interactive setup flow: capturing the browser redirect, and failing cleanly
when prerequisites are missing.
"""

from __future__ import annotations

import threading
from http.server import HTTPServer

import requests

import resideo_oauth_setup as setup


def test_callback_handler_captures_authorization_code():
    setup._CallbackHandler.authorization_code = None
    server = HTTPServer(("localhost", 0), setup._CallbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    response = requests.get(f"http://localhost:{port}/callback?code=abc123", timeout=5)
    thread.join(timeout=5)
    server.server_close()

    assert response.status_code == 200
    assert setup._CallbackHandler.authorization_code == "abc123"


def test_callback_handler_handles_missing_code():
    setup._CallbackHandler.authorization_code = "leftover-from-previous-test"
    server = HTTPServer(("localhost", 0), setup._CallbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    requests.get(f"http://localhost:{port}/callback", timeout=5)
    thread.join(timeout=5)
    server.server_close()

    assert setup._CallbackHandler.authorization_code is None


def test_run_fails_fast_without_client_credentials(capsys):
    exit_code = setup.run({"resideo": {}}, port=0)

    assert exit_code == 1
    assert "client_id/client_secret" in capsys.readouterr().out
