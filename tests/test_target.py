"""Tests for the target parsing module."""

import os
import subprocess

import pytest

from bubble.repo_registry import RepoRegistry
from bubble.target import (
    Target,
    TargetParseError,
    _git_repo_info,
    _parse_github_remote,
    _parse_local_path,
    parse_target,
)


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "repos.json"
    reg = RepoRegistry(path)
    reg.register("leanprover-community", "mathlib4")
    reg.register("leanprover", "lean4")
    return reg


@pytest.fixture
def empty_registry(tmp_path):
    path = tmp_path / "repos.json"
    return RepoRegistry(path)


class TestParseFullURL:
    def test_pr_url(self, registry):
        t = parse_target("https://github.com/leanprover-community/mathlib4/pull/35219", registry)
        assert t.owner == "leanprover-community"
        assert t.repo == "mathlib4"
        assert t.kind == "pr"
        assert t.ref == "35219"

    def test_branch_url(self, registry):
        t = parse_target("https://github.com/leanprover/lean4/tree/master", registry)
        assert t.owner == "leanprover"
        assert t.repo == "lean4"
        assert t.kind == "branch"
        assert t.ref == "master"

    def test_branch_with_slashes(self, registry):
        t = parse_target(
            "https://github.com/leanprover/lean4/tree/feat/some-feature", registry
        )
        assert t.kind == "branch"
        assert t.ref == "feat/some-feature"

    def test_commit_url(self, registry):
        t = parse_target(
            "https://github.com/leanprover/lean4/commit/abc123def456", registry
        )
        assert t.owner == "leanprover"
        assert t.repo == "lean4"
        assert t.kind == "commit"
        assert t.ref == "abc123def456"

    def test_repo_url(self, registry):
        t = parse_target("https://github.com/leanprover/lean4", registry)
        assert t.owner == "leanprover"
        assert t.repo == "lean4"
        assert t.kind == "repo"
        assert t.ref == ""

    def test_http_url(self, registry):
        t = parse_target("http://github.com/leanprover/lean4/pull/123", registry)
        assert t.kind == "pr"
        assert t.ref == "123"

    def test_trailing_slash(self, registry):
        t = parse_target("https://github.com/leanprover/lean4/", registry)
        assert t.kind == "repo"

    def test_pr_url_with_extra_path(self, registry):
        # GitHub PR URLs sometimes have /files or /commits suffix
        t = parse_target(
            "https://github.com/leanprover-community/mathlib4/pull/35219/files", registry
        )
        assert t.kind == "pr"
        assert t.ref == "35219"


class TestParsePartialURL:
    def test_no_scheme(self, registry):
        t = parse_target("github.com/leanprover/lean4/pull/123", registry)
        assert t.kind == "pr"
        assert t.owner == "leanprover"

    def test_no_host(self, registry):
        t = parse_target("leanprover-community/mathlib4/pull/35219", registry)
        assert t.kind == "pr"
        assert t.owner == "leanprover-community"
        assert t.ref == "35219"

    def test_owner_repo_only(self, registry):
        t = parse_target("leanprover/lean4", registry)
        assert t.kind == "repo"
        assert t.owner == "leanprover"
        assert t.repo == "lean4"


class TestParseShortName:
    def test_short_name_repo(self, registry):
        t = parse_target("mathlib4", registry)
        assert t.owner == "leanprover-community"
        assert t.repo == "mathlib4"
        assert t.kind == "repo"

    def test_short_name_pr(self, registry):
        t = parse_target("mathlib4/pull/123", registry)
        assert t.owner == "leanprover-community"
        assert t.repo == "mathlib4"
        assert t.kind == "pr"
        assert t.ref == "123"

    def test_short_name_branch(self, registry):
        t = parse_target("lean4/tree/some-branch", registry)
        assert t.owner == "leanprover"
        assert t.repo == "lean4"
        assert t.kind == "branch"
        assert t.ref == "some-branch"

    def test_unknown_short_name(self, empty_registry):
        with pytest.raises(TargetParseError, match="Unknown repo"):
            parse_target("unknown", empty_registry)

    def test_ambiguous_short_name(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)
        reg.register("alice", "utils")
        reg.register("bob", "utils")

        with pytest.raises(TargetParseError, match="ambiguous"):
            parse_target("utils", reg)


class TestParseErrors:
    def test_empty_target(self, registry):
        with pytest.raises(TargetParseError, match="Empty"):
            parse_target("", registry)

    def test_invalid_pr_number(self, registry):
        with pytest.raises(TargetParseError, match="Invalid PR number"):
            parse_target("leanprover/lean4/pull/notanumber", registry)


class TestTargetProperties:
    def test_org_repo(self, registry):
        t = parse_target("leanprover/lean4", registry)
        assert t.org_repo == "leanprover/lean4"

    def test_short_name(self, registry):
        t = parse_target("leanprover-community/mathlib4", registry)
        assert t.short_name == "mathlib4"

    def test_original_preserved(self, registry):
        raw = "https://github.com/leanprover/lean4/pull/123"
        t = parse_target(raw, registry)
        assert t.original == raw

    def test_local_path_default_empty(self, registry):
        t = parse_target("leanprover/lean4", registry)
        assert t.local_path == ""


# ---------------------------------------------------------------------------
# Helpers for local git repo fixtures
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def _make_git_repo(tmp_path, *, remote_url="https://github.com/testowner/testrepo.git",
                   branch="main", dirty=False, detached=False, commit=True):
    """Create a local git repo with a fake remote for testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**_GIT_ENV, "HOME": str(tmp_path)}

    subprocess.run(["git", "init", "-b", branch, str(repo)],
                   capture_output=True, check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote_url],
                   capture_output=True, check=True, env=env)

    if commit:
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "-C", str(repo), "add", "."],
                       capture_output=True, check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                       capture_output=True, check=True, env=env)

    if detached and commit:
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", sha],
                       capture_output=True, check=True, env=env)

    if dirty:
        (repo / "dirty.txt").write_text("uncommitted\n")

    return repo


# ---------------------------------------------------------------------------
# Test: _parse_github_remote
# ---------------------------------------------------------------------------


class TestParseGithubRemote:
    def test_https_url(self):
        owner, repo = _parse_github_remote("https://github.com/leanprover/lean4.git")
        assert owner == "leanprover"
        assert repo == "lean4"

    def test_https_url_no_dotgit(self):
        owner, repo = _parse_github_remote("https://github.com/leanprover/lean4")
        assert owner == "leanprover"
        assert repo == "lean4"

    def test_ssh_url(self):
        owner, repo = _parse_github_remote("git@github.com:leanprover/lean4.git")
        assert owner == "leanprover"
        assert repo == "lean4"

    def test_ssh_url_no_dotgit(self):
        owner, repo = _parse_github_remote("git@github.com:leanprover/lean4")
        assert owner == "leanprover"
        assert repo == "lean4"

    def test_non_github_url(self):
        with pytest.raises(TargetParseError, match="not a GitHub repository"):
            _parse_github_remote("https://gitlab.com/owner/repo.git")


# ---------------------------------------------------------------------------
# Test: _parse_local_path
# ---------------------------------------------------------------------------


class TestParseLocalPath:
    def test_dot_current_dir(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        monkeypatch.chdir(repo)
        t = _parse_local_path(".")
        assert t.owner == "testowner"
        assert t.repo == "testrepo"
        assert t.kind == "branch"
        assert t.ref == "main"
        assert t.local_path == str(repo)

    def test_relative_path(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        t = _parse_local_path("./repo")
        assert t.owner == "testowner"
        assert t.repo == "testrepo"
        assert t.local_path == str(repo)

    def test_absolute_path(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        t = _parse_local_path(str(repo))
        assert t.owner == "testowner"
        assert t.repo == "testrepo"

    def test_subdirectory(self, tmp_path):
        """Pointing at a subdirectory resolves to the repo root."""
        repo = _make_git_repo(tmp_path)
        subdir = repo / "src"
        subdir.mkdir()
        t = _parse_local_path(str(subdir))
        assert t.local_path == str(repo)

    def test_not_a_git_repo(self, tmp_path):
        not_repo = tmp_path / "notrepo"
        not_repo.mkdir()
        with pytest.raises(TargetParseError, match="not a git repository"):
            _parse_local_path(str(not_repo))

    def test_path_does_not_exist(self, tmp_path):
        with pytest.raises(TargetParseError, match="does not exist"):
            _parse_local_path(str(tmp_path / "nonexistent"))

    def test_dirty_working_tree(self, tmp_path):
        repo = _make_git_repo(tmp_path, dirty=True)
        with pytest.raises(TargetParseError, match="uncommitted changes"):
            _parse_local_path(str(repo))

    def test_no_remote(self, tmp_path):
        repo = tmp_path / "norepo"
        repo.mkdir()
        env = {**_GIT_ENV, "HOME": str(tmp_path)}
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True, env=env)
        (repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "."],
                       capture_output=True, check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                       capture_output=True, check=True, env=env)
        with pytest.raises(TargetParseError, match="No remote"):
            _parse_local_path(str(repo))

    def test_detached_head(self, tmp_path):
        repo = _make_git_repo(tmp_path, detached=True)
        with pytest.raises(TargetParseError, match="detached"):
            _parse_local_path(str(repo))

    def test_non_github_remote(self, tmp_path):
        repo = _make_git_repo(tmp_path, remote_url="https://gitlab.com/owner/repo.git")
        with pytest.raises(TargetParseError, match="not a GitHub repository"):
            _parse_local_path(str(repo))

    def test_ssh_remote_url(self, tmp_path):
        repo = _make_git_repo(tmp_path, remote_url="git@github.com:myorg/myrepo.git")
        t = _parse_local_path(str(repo))
        assert t.owner == "myorg"
        assert t.repo == "myrepo"

    def test_unpushed_branch_ok(self, tmp_path):
        """Unpushed branches are fine — local objects shared via --reference."""
        repo = _make_git_repo(tmp_path, branch="feature-branch")
        t = _parse_local_path(str(repo))
        assert t.ref == "feature-branch"
        assert t.local_path == str(repo)


# ---------------------------------------------------------------------------
# Test: bare number PR parsing
# ---------------------------------------------------------------------------


class TestParseBareNumber:
    def test_bare_number_in_git_repo(self, tmp_path, monkeypatch, registry):
        repo = _make_git_repo(tmp_path,
                              remote_url="https://github.com/leanprover/lean4.git")
        monkeypatch.chdir(repo)
        t = parse_target("123", registry)
        assert t.owner == "leanprover"
        assert t.repo == "lean4"
        assert t.kind == "pr"
        assert t.ref == "123"
        assert t.local_path == ""  # bare number doesn't set local_path

    def test_bare_number_not_in_git_repo(self, tmp_path, monkeypatch, empty_registry):
        not_repo = tmp_path / "notrepo"
        not_repo.mkdir()
        monkeypatch.chdir(not_repo)
        with pytest.raises(TargetParseError, match="looks like a PR number"):
            parse_target("123", empty_registry)

    def test_bare_number_with_ssh_remote(self, tmp_path, monkeypatch, registry):
        repo = _make_git_repo(tmp_path,
                              remote_url="git@github.com:myorg/myrepo.git")
        monkeypatch.chdir(repo)
        t = parse_target("456", registry)
        assert t.owner == "myorg"
        assert t.repo == "myrepo"
        assert t.kind == "pr"
        assert t.ref == "456"


# ---------------------------------------------------------------------------
# Test: parse_target with local paths
# ---------------------------------------------------------------------------


class TestParseTargetLocalPaths:
    def test_dot_routes_to_local(self, tmp_path, monkeypatch, registry):
        repo = _make_git_repo(tmp_path)
        monkeypatch.chdir(repo)
        t = parse_target(".", registry)
        assert t.kind == "branch"
        assert t.local_path == str(repo)

    def test_dotslash_routes_to_local(self, tmp_path, monkeypatch, registry):
        repo = _make_git_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        t = parse_target("./repo", registry)
        assert t.kind == "branch"
        assert t.local_path == str(repo)

    def test_absolute_path_routes_to_local(self, tmp_path, registry):
        repo = _make_git_repo(tmp_path)
        t = parse_target(str(repo), registry)
        assert t.kind == "branch"
        assert t.local_path == str(repo)

    def test_unknown_shortname_suggests_path(self, empty_registry):
        with pytest.raises(TargetParseError, match="local path.*--path"):
            parse_target("unknown", empty_registry)
