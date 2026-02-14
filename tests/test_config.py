"""Tests for configuration management."""

import pytest

from bubble.config import _deep_merge, repo_short_name, resolve_repo


def test_resolve_repo_mathlib():
    assert resolve_repo("mathlib4") == "leanprover-community/mathlib4"


def test_resolve_repo_mathlib_alias():
    assert resolve_repo("mathlib") == "leanprover-community/mathlib4"


def test_resolve_repo_lean4():
    assert resolve_repo("lean4") == "leanprover/lean4"


def test_resolve_repo_lean_alias():
    assert resolve_repo("lean") == "leanprover/lean4"


def test_resolve_repo_batteries():
    assert resolve_repo("batteries") == "leanprover-community/batteries"


def test_resolve_repo_passthrough():
    assert resolve_repo("myorg/myrepo") == "myorg/myrepo"


def test_resolve_repo_unknown():
    with pytest.raises(ValueError, match="Unknown repo"):
        resolve_repo("nonexistent")


def test_repo_short_name():
    assert repo_short_name("leanprover-community/mathlib4") == "mathlib4"


def test_repo_short_name_lowercases():
    assert repo_short_name("org/MyRepo") == "myrepo"


def test_deep_merge_basic():
    base = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_nested():
    base = {"x": {"a": 1, "b": 2}}
    override = {"x": {"b": 3, "c": 4}}
    result = _deep_merge(base, override)
    assert result == {"x": {"a": 1, "b": 3, "c": 4}}


def test_deep_merge_override_replaces_non_dict():
    base = {"x": {"a": 1}}
    override = {"x": "replaced"}
    result = _deep_merge(base, override)
    assert result == {"x": "replaced"}


def test_load_config_creates_default(tmp_data_dir):
    from bubble.config import load_config

    config = load_config()
    assert config["runtime"]["backend"] == "incus"
    assert "github.com" in config["network"]["allowlist"]


def test_save_load_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["runtime"]["colima_cpu"] = 42
    save_config(config)

    reloaded = load_config()
    assert reloaded["runtime"]["colima_cpu"] == 42
