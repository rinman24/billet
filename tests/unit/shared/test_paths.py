"""Tests for config-path resolution."""

from pathlib import Path

import pytest

from billet.shared.paths import default_config_path, resolve_config_path


def test_explicit_path_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BILLET_CONFIG", str(tmp_path / "env.toml"))
    assert resolve_config_path(tmp_path / "explicit.toml") == tmp_path / "explicit.toml"


def test_env_var_used_when_no_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "env.toml"
    monkeypatch.setenv("BILLET_CONFIG", str(target))
    assert resolve_config_path(None) == target


def test_xdg_default_when_nothing_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BILLET_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    expected = tmp_path / "billet" / "config.toml"
    assert default_config_path() == expected
    assert resolve_config_path(None) == expected
