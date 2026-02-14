"""Tests for shared git store path/URL generation."""

from bubble.git_store import bare_repo_path, github_url


def test_bare_repo_path_lean4(tmp_data_dir):
    path = bare_repo_path("leanprover/lean4")
    assert path.name == "lean4.git"


def test_bare_repo_path_mathlib(tmp_data_dir):
    path = bare_repo_path("leanprover-community/mathlib4")
    assert path.name == "mathlib4.git"


def test_github_url():
    assert github_url("leanprover/lean4") == "https://github.com/leanprover/lean4.git"


def test_github_url_community():
    url = github_url("leanprover-community/mathlib4")
    assert url == "https://github.com/leanprover-community/mathlib4.git"
