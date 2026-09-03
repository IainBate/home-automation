"""Shared pytest fixtures/setup.

scripts/*.py are entry-point modules, not a package - they rely on being
launched with `python3 scripts/foo.py`, which puts scripts/ on sys.path[0]
automatically. Tests need the same thing done explicitly so `import
hotwater_automation_core` (and similar) resolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _isolate_shared_caches(tmp_path_factory, monkeypatch):
    """Point the process-wide cache files at a temp dir for every test.

    The Ohme status cache and the MELCloud token cache are read from fixed
    paths under config/, and on the Pi those files really exist and are kept
    fresh by the running daemons. Without this, tests that mean to exercise a
    direct API call silently take the cache path instead - passing on a dev
    machine (no cache files) and failing on the Pi, which is exactly what
    happened to tests/scripts/test_hotwater_is_car_charging.py the first time
    this was deployed.

    Redirects the paths rather than stubbing the read functions, so the real
    cache code still runs (and tests that patch those functions themselves
    still win, since their patch is applied inside this one).

    Uses its own directory rather than the test's `tmp_path`, so a test that
    asserts on the exact contents of its own tmp_path isn't tripped up by
    this fixture's files.
    """
    cache_dir = tmp_path_factory.mktemp("shared-caches")

    from src.api_clients import melcloud_token_cache, ohme_status_cache

    monkeypatch.setattr(
        ohme_status_cache, "get_ohme_status_path", lambda: str(cache_dir / "ohme_status.json")
    )
    monkeypatch.setattr(
        melcloud_token_cache,
        "get_melcloud_token_cache_path",
        lambda: str(cache_dir / "melcloud_token.json"),
    )
