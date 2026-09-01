"""Unit tests for src/utils/state_store.py - the shared locked-JSON-state primitive.

This is the primitive that closes a real race: a read-then-later-write
across a slow decide step (hardware/API calls in between) isn't atomic, so
two concurrent callers can each read "clear", both act, and the second write
silently clobbers the first's update. These tests pin down the properties
that matter for that guarantee - persistence, corrupt/missing-file handling,
no write on exception, and mutual exclusion under real concurrency.
"""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

from src.utils import state_store
from src.utils.state_store import locked_json_state, read_json_state, write_json_atomic


def test_read_json_state_missing_file_returns_empty_dict(tmp_path):
    assert read_json_state(tmp_path / "missing.json") == {}


def test_read_json_state_corrupt_file_returns_empty_dict(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_json_state(path) == {}


def test_read_json_state_reads_existing_content(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    assert read_json_state(path) == {"foo": "bar"}


def test_locked_json_state_creates_missing_file_and_parent_dir(tmp_path):
    path = tmp_path / "nested" / "state.json"
    with locked_json_state(path) as state:
        assert state == {}
        state["created"] = True
    assert read_json_state(path) == {"created": True}


def test_locked_json_state_persists_mutation_on_clean_exit(tmp_path):
    path = tmp_path / "state.json"
    with locked_json_state(path) as state:
        state["count"] = 1
    with locked_json_state(path) as state:
        state["count"] += 1
    assert read_json_state(path) == {"count": 2}


def test_locked_json_state_does_not_write_on_exception(tmp_path):
    path = tmp_path / "state.json"
    with locked_json_state(path) as state:
        state["initial"] = True

    class DeliberateError(Exception):
        pass

    try:
        with locked_json_state(path) as state:
            state["should_not_persist"] = True
            raise DeliberateError
    except DeliberateError:
        pass

    assert read_json_state(path) == {"initial": True}


def test_locked_json_state_persists_on_early_return_from_caller(tmp_path):
    """Mirrors the real usage pattern: mutate then `return` from inside the `with` block."""
    path = tmp_path / "state.json"

    def act() -> str:
        with locked_json_state(path) as state:
            state["acted"] = True
            return "done"

    assert act() == "done"
    assert read_json_state(path) == {"acted": True}


def test_locked_json_state_recovers_from_corrupt_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    with locked_json_state(path) as state:
        assert state == {}
        state["recovered"] = True
    assert read_json_state(path) == {"recovered": True}


def test_locked_json_state_serializes_concurrent_writers(tmp_path):
    """Two threads incrementing the same counter must not lose an update to the race."""
    path = tmp_path / "state.json"
    with locked_json_state(path) as state:
        state["count"] = 0

    def increment_slowly() -> None:
        with locked_json_state(path) as state:
            current = state["count"]
            time.sleep(0.05)  # simulate the slow hardware/API decide step
            state["count"] = current + 1

    threads = [threading.Thread(target=increment_slowly) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert read_json_state(path)["count"] == 5


def test_locked_json_state_raises_timeout_when_lock_held(tmp_path):
    path = tmp_path / "state.json"
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_lock() -> None:
        with locked_json_state(path) as state:
            state["held"] = True
            holder_ready.set()
            release_holder.wait(timeout=5)

    holder_thread = threading.Thread(target=hold_lock)
    holder_thread.start()
    holder_ready.wait(timeout=5)

    try:
        raised = False
        try:
            with locked_json_state(path, timeout=0.2):
                pass
        except TimeoutError:
            raised = True
        assert raised
    finally:
        release_holder.set()
        holder_thread.join()


def test_locked_json_state_skips_write_when_state_is_never_mutated(tmp_path):
    """Regression test: the write+fsync used to happen unconditionally on
    every clean exit, even for a plain read that never changed anything -
    the common case for most callers (e.g. a "no action needed" check).
    """
    path = tmp_path / "state.json"
    with locked_json_state(path) as state:
        state["existing"] = "value"

    with mock.patch.object(state_store.os, "fsync") as fake_fsync, mock.patch.object(
        state_store.json, "dump"
    ) as fake_dump:
        with locked_json_state(path) as state:
            _ = state.get("existing")  # read-only, no mutation

        fake_dump.assert_not_called()
        fake_fsync.assert_not_called()


def test_locked_json_state_skips_write_when_mutation_nets_to_no_change(tmp_path):
    """Even a mutate-then-revert-back-to-the-same-value nets to "unchanged" -
    the comparison is by final content, not by whether any assignment happened.
    """
    path = tmp_path / "state.json"
    with locked_json_state(path) as state:
        state["count"] = 1

    with mock.patch.object(state_store.os, "fsync") as fake_fsync:
        with locked_json_state(path) as state:
            state["count"] = 2
            state["count"] = 1  # back to the original value

        fake_fsync.assert_not_called()

    assert read_json_state(path) == {"count": 1}


def test_locked_json_state_still_writes_when_state_actually_changes(tmp_path):
    path = tmp_path / "state.json"
    with locked_json_state(path) as state:
        state["count"] = 1

    with mock.patch.object(state_store.os, "fsync") as fake_fsync:
        with locked_json_state(path) as state:
            state["count"] = 2

        fake_fsync.assert_called_once()

    assert read_json_state(path) == {"count": 2}
