"""Root logging setup for cron-driven one-shot CLI scripts.

Plain `logging.basicConfig()` attaches a StreamHandler to the root logger,
so any WARNING+ record from the script itself OR any module it imports
(e.g. src.api_clients.*) prints to stderr. That's the right behaviour for a
script a person runs by hand - but for a script cron runs unattended
(claude_usage_poller.py, mg_saic_poller.py), stderr output makes cron's
MAILTO mail it immediately, once per poll cycle, with no batching and no
way to tell a one-off blip from a sustained problem.

configure_cron_safe_logging() routes those same records to a rotating file
instead, whenever the script was invoked with --quiet (which the crontab
entries for these pollers already pass) - cron then sees no output and
sends no mail. scripts/daily_digest_check.py sweeps that file once a day
and reports only what recurred, batching and squelching in one place
instead of leaving every future cron-driven poller to reinvent it.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_cron_safe_logging(*, level: int, quiet: bool, log_filename: str) -> None:
    """Configure root logging: console when run by hand, file-only when --quiet (cron).

    Args:
        level: Root logger level (and the level below which nothing is handled at all).
        quiet: True to log to a rotating file under logs/ instead of stderr - pass the
            script's own --quiet flag here.
        log_filename: Bare filename under logs/ to use when quiet (e.g. "claude_usage_poller.log").

    """
    fmt = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

    if not quiet:
        logging.basicConfig(level=level, format=fmt)
        return

    Path("logs").mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        f"logs/{log_filename}", when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(fmt))
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)
