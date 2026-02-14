"""Tests for the target parsing module."""

import pytest

from bubble.repo_registry import RepoRegistry
from bubble.target import Target, TargetParseError, parse_target


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
