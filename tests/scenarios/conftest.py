"""Makes tests/api_clients/'s fake-server helper modules (solax_fake_server,
ohme_fake_server) importable from tests/scenarios/ - mirrors the root
tests/conftest.py's sys.path trick for scripts/.
"""

from __future__ import annotations

import sys
from pathlib import Path

API_CLIENTS_TESTS_DIR = Path(__file__).resolve().parent.parent / "api_clients"
if str(API_CLIENTS_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(API_CLIENTS_TESTS_DIR))
