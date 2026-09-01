"""Tests for scripts/common_config_utils.py's config-file search order.

find_config_file searches cwd-relative paths (./config.yaml, ../config.yaml,
../../config.yaml), so these tests control the search via monkeypatch.chdir
into a controlled temp directory tree rather than mocking Path.is_file.
"""

from __future__ import annotations

from common_config_utils import find_config_file, load_config_with_fallback


def test_explicit_config_arg_that_exists_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("a: 1", encoding="utf-8")

    assert find_config_file(str(explicit)) == str(explicit)


def test_explicit_config_arg_that_does_not_exist_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert find_config_file(str(tmp_path / "missing.yaml")) is None


def test_finds_config_in_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")

    assert find_config_file() == "./config.yaml"


def test_finds_config_one_level_up(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")
    subdir = tmp_path / "scripts"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    assert find_config_file() == "../config.yaml"


def test_finds_config_two_levels_up(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert find_config_file() == "../../config.yaml"


def test_returns_none_when_no_config_found_anywhere(tmp_path, monkeypatch):
    empty_dir = tmp_path / "nowhere"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)

    assert find_config_file() is None


def test_load_config_with_fallback_returns_none_when_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config_with_fallback() is None


def test_load_config_with_fallback_uses_loader_func(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")

    result = load_config_with_fallback(loader_func=lambda path: {"loaded_from": path})

    assert result == {"loaded_from": "./config.yaml"}


def test_load_config_with_fallback_returns_path_when_no_loader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")

    assert load_config_with_fallback() == "./config.yaml"


def test_load_config_with_fallback_handles_loader_exception(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")

    def broken_loader(_path):
        raise ValueError("bad config")

    assert load_config_with_fallback(loader_func=broken_loader) is None


def test_load_config_with_fallback_handles_loader_returning_falsy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("a: 1", encoding="utf-8")

    assert load_config_with_fallback(loader_func=lambda path: None) is None
