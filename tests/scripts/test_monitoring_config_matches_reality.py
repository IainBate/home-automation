"""Structural checks that config.yaml's monitoring lists match what exists.

A typo in either list fails silently and in the worst possible direction: the
thing you thought you were watching simply isn't watched, and you find out
when something breaks unnoticed. (Both lists have already been wrong in
practice - the hot water service was missing from systemd_services for as
long as it had been deployed, and a log filename here was mistyped the day
it was added.)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _config() -> dict:
    with (PROJECT_ROOT / "config.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _log_filenames_the_code_actually_writes() -> set[str]:
    """Every log filename passed to this project's two logging setup helpers."""
    names: set[str] = set()
    pattern = re.compile(r'log_filename\s*=\s*"([^"]+)"|"([^"]+\.log)"')
    for path in list(SCRIPTS_DIR.glob("*.py")) + list((PROJECT_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "setup_rotating_logger" not in text and "configure_cron_safe_logging" not in text:
            continue
        for match in pattern.finditer(text):
            name = match.group(1) or match.group(2)
            if name and name.endswith(".log"):
                names.add(name)
    return names


def test_every_watched_log_file_is_one_something_actually_writes():
    watched = set(_config().get("daily_digest_check", {}).get("log_files", []))
    written = _log_filenames_the_code_actually_writes()

    unknown = watched - written
    assert not unknown, (
        f"daily_digest_check.log_files names {sorted(unknown)}, which no script writes - "
        f"those would be silently unmonitored. Known log files: {sorted(written)}"
    )


def test_every_deployed_service_unit_file_exists_in_the_repo():
    watched = set(_config().get("weekly_health_check", {}).get("systemd_services", []))
    shipped = {p.name for p in SCRIPTS_DIR.glob("*.service")}

    missing = watched - shipped
    assert not missing, (
        f"weekly_health_check.systemd_services names {sorted(missing)}, which has no unit file "
        f"in scripts/. Shipped units: {sorted(shipped)}"
    )


def test_every_shipped_service_unit_is_health_checked():
    """The reverse: a service deployed but not listed is one nobody notices dying."""
    shipped = {p.name for p in SCRIPTS_DIR.glob("*.service")}
    watched = set(_config().get("weekly_health_check", {}).get("systemd_services", []))

    unwatched = shipped - watched
    assert not unwatched, (
        f"scripts/ ships {sorted(unwatched)} but weekly_health_check.systemd_services "
        f"doesn't list them - they'd go unmonitored"
    )
