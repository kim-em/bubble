"""Tests for the repo registry module."""

import json

from bubble.repo_registry import RepoRegistry


class TestRepoRegistry:
    def test_register_and_resolve(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)

        reg.register("leanprover-community", "mathlib4")
        assert reg.resolve("mathlib4") == "leanprover-community/mathlib4"

    def test_resolve_unknown(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)

        assert reg.resolve("unknown") is None

    def test_resolve_case_insensitive(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)

        reg.register("leanprover", "Lean4")
        assert reg.resolve("lean4") == "leanprover/Lean4"
        assert reg.resolve("Lean4") == "leanprover/Lean4"

    def test_ambiguity_detection(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)

        reg.register("alice", "utils")
        assert not reg.is_ambiguous("utils")

        reg.register("bob", "utils")
        assert reg.is_ambiguous("utils")
        assert reg.resolve("utils") is None

        options = reg.get_ambiguous_options("utils")
        assert "alice/utils" in options
        assert "bob/utils" in options

    def test_same_repo_not_ambiguous(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)

        reg.register("leanprover", "lean4")
        reg.register("leanprover", "lean4")
        assert not reg.is_ambiguous("lean4")
        assert reg.resolve("lean4") == "leanprover/lean4"

    def test_persistence(self, tmp_path):
        path = tmp_path / "repos.json"
        reg1 = RepoRegistry(path)
        reg1.register("leanprover", "lean4")

        reg2 = RepoRegistry(path)
        assert reg2.resolve("lean4") == "leanprover/lean4"

    def test_json_format(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)
        reg.register("leanprover", "lean4")

        data = json.loads(path.read_text())
        assert "repos" in data
        assert "ambiguous" in data
        assert "lean4" in data["repos"]
        assert data["repos"]["lean4"]["owner"] == "leanprover"
        assert data["repos"]["lean4"]["repo"] == "lean4"

    def test_list_all(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)
        reg.register("leanprover", "lean4")
        reg.register("leanprover-community", "mathlib4")

        all_repos = reg.list_all()
        assert all_repos == {
            "lean4": "leanprover/lean4",
            "mathlib4": "leanprover-community/mathlib4",
        }

    def test_third_ambiguous_entry(self, tmp_path):
        path = tmp_path / "repos.json"
        reg = RepoRegistry(path)

        reg.register("alice", "utils")
        reg.register("bob", "utils")
        reg.register("charlie", "utils")

        options = reg.get_ambiguous_options("utils")
        assert len(options) == 3
        assert "charlie/utils" in options
