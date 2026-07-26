"""Tests for shared git store path/URL generation."""

import subprocess

import pytest

from bubble import git_store
from bubble.git_store import bare_repo_path, github_url


def test_bare_repo_path_lean4(tmp_data_dir):
    path = bare_repo_path("leanprover/lean4")
    assert path.name == "lean4.git"
    assert path.parent.name == "leanprover"


def test_bare_repo_path_mathlib(tmp_data_dir):
    path = bare_repo_path("leanprover-community/mathlib4")
    assert path.name == "mathlib4.git"
    assert path.parent.name == "leanprover-community"


def test_same_short_name_different_owners_do_not_collide(tmp_data_dir):
    assert bare_repo_path("one/project") != bare_repo_path("two/project")


def test_same_short_name_different_owners_use_distinct_locks(tmp_data_dir):
    with git_store._repo_lock("one/project"):
        pass
    with git_store._repo_lock("two/project"):
        pass
    assert len(list(git_store.GIT_LOCK_DIR.glob("*.lock"))) == 2


def test_github_url():
    assert github_url("leanprover/lean4") == "https://github.com/leanprover/lean4.git"


def test_github_url_community():
    url = github_url("leanprover-community/mathlib4")
    assert url == "https://github.com/leanprover-community/mathlib4.git"


@pytest.mark.parametrize("value", ["owner/..", "../repo", "owner/repo/extra", "owner/re po"])
def test_invalid_repository_rejected(value, tmp_data_dir):
    with pytest.raises(ValueError):
        bare_repo_path(value)


def test_legacy_mirror_migrates_when_origin_matches(tmp_data_dir):
    legacy = git_store.LEGACY_GIT_DIR / "demo.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(legacy)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(legacy),
            "remote",
            "add",
            "origin",
            "https://github.com/Owner/Demo.git",
        ],
        check=True,
    )

    destination = git_store.init_bare_repo("owner/demo")

    assert destination.is_dir()
    assert legacy.is_symlink()
    assert legacy.resolve() == destination
    assert destination == bare_repo_path("owner/demo")


def test_mismatched_legacy_mirror_is_not_adopted(tmp_data_dir):
    legacy = git_store.LEGACY_GIT_DIR / "demo.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(legacy)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(legacy),
            "remote",
            "add",
            "origin",
            "https://github.com/someone-else/demo.git",
        ],
        check=True,
    )

    assert not git_store._migrate_legacy_mirror("owner/demo", bare_repo_path("owner/demo"))
    assert legacy.exists()


def test_migration_finds_mixed_case_legacy_name(tmp_data_dir):
    legacy = git_store.LEGACY_GIT_DIR / "Demo.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(legacy)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(legacy),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/demo.git",
        ],
        check=True,
    )
    assert git_store.init_bare_repo("owner/demo") == bare_repo_path("owner/demo")
    assert legacy.is_symlink()


def test_cross_filesystem_migration_leaves_legacy_mirror(tmp_data_dir, monkeypatch):
    legacy = git_store.LEGACY_GIT_DIR / "demo.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(legacy)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(legacy),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/demo.git",
        ],
        check=True,
    )
    monkeypatch.setattr(git_store.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError()))

    assert not git_store._migrate_legacy_mirror("owner/demo", bare_repo_path("owner/demo"))
    assert legacy.is_dir()


def test_origin_lookup_does_not_discover_parent_repo(tmp_data_dir):
    not_a_repo = tmp_data_dir / "inside-parent"
    not_a_repo.mkdir()
    assert git_store._origin_repo(not_a_repo) is None


def test_repo_is_known_finds_verified_legacy_mirror(tmp_data_dir):
    legacy = git_store.LEGACY_GIT_DIR / "demo.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(legacy)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(legacy),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/demo.git",
        ],
        check=True,
    )
    assert git_store.repo_is_known("owner/demo")
