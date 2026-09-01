"""Race-free JSON state file persistence, shared by the project's daemons.

Both hotwater_mode_daemon.py (via scripts/hotwater_automation_core.py) and
battery_mode_daemon.py persist a small JSON blob across runs/cycles - "when
did we last change mode", "is a legionella cycle in progress", etc. Both need
the same property: a read-modify-write that spans a check's real
hardware/API decide step (which can take several seconds) must be atomic
against a concurrent process/cycle doing the same thing to the same file, or
one call's result silently overwrites the other's more recent update. A
plain read-then-later-write is NOT atomic across that gap. This module holds
the fcntl-based locking that closes it in exactly one place, so there is one
race-safety story rather than a different one per daemon.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time as time_module
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


def read_json_state(path: str | Path) -> dict[str, Any]:
    """Read a JSON state file, or {} if it's absent/unreadable/corrupt."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@contextlib.contextmanager
def locked_json_state(
    path: str | Path, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[dict[str, Any]]:
    """Exclusive, race-free read-modify-write of a JSON state file.

    The motivating race: caller A reads the file, does slow I/O (a hardware
    request-then-verify retry loop, say), then writes an update. If caller B
    does the same thing concurrently, whichever finishes last overwrites the
    file with a copy of state that was already stale when it started,
    silently erasing the other's update. Holding an exclusive lock across the
    whole read-decide-write span - not just the final write - closes that
    window.

    Callers should do their slow I/O *before* entering this block where
    possible, and use the block itself for the fast "re-read current state,
    merge in my update" step - though a caller that must hold the lock across
    its own slow retry loop too (as scripts/hotwater_automation_core.py's
    force-heat/legionella checks do, to stay mutually exclusive with each
    other) is exactly what the timeout parameter is for.

    Yields:
        The current state dict - mutate it in place; it's written back
        automatically when the block exits normally, unless the block never
        actually changed it (a plain read is common - most checks are
        no-ops most of the time), in which case the write+fsync is skipped
        entirely. Nothing is written if the block raises.

    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")

    start_time = time_module.time()
    with path.open("r+", encoding="utf-8") as fd:
        while time_module.time() - start_time < timeout:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time_module.sleep(0.05)
        else:
            msg = f"Could not acquire lock on {path} within {timeout} seconds"
            raise TimeoutError(msg)

        try:
            fd.seek(0)
            try:
                state = json.load(fd)
            except json.JSONDecodeError:
                state = {}

            # sort_keys so key-order alone (never semantically meaningful for
            # these state dicts) can't cause a false "changed" positive - this
            # snapshot is only ever used for the comparison below, never written.
            before = json.dumps(state, sort_keys=True)

            yield state

            if json.dumps(state, sort_keys=True) == before:
                # Most checks (run_force_heat_check's common "no action
                # needed" case, say) never mutate state at all - skip the
                # write+fsync entirely rather than rewriting byte-identical
                # content to disk every single poll cycle.
                return

            fd.seek(0)
            fd.truncate()
            json.dump(state, fd)
            fd.flush()
            os.fsync(fd.fileno())
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
