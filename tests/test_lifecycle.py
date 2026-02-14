"""Tests for lifecycle management (registry, archive, reconstitute)."""

import json

from bubble.lifecycle import (
    archive_bubble,
    check_git_synced,
    get_bubble_info,
    register_bubble,
)


class TestRegistry:
    def test_register_and_get(self, tmp_data_dir):
        register_bubble("test-bubble", "org/repo", branch="main", commit="abc123")
        info = get_bubble_info("test-bubble")
        assert info is not None
        assert info["org_repo"] == "org/repo"
        assert info["branch"] == "main"
        assert info["commit"] == "abc123"
        assert info["state"] == "active"

    def test_get_unknown_returns_none(self, tmp_data_dir):
        assert get_bubble_info("nonexistent") is None

    def test_registry_is_valid_json(self, tmp_data_dir):
        register_bubble("b1", "org/repo1")
        register_bubble("b2", "org/repo2")
        import bubble.config as config

        content = config.REGISTRY_FILE.read_text()
        data = json.loads(content)
        assert "bubbles" in data
        assert "b1" in data["bubbles"]
        assert "b2" in data["bubbles"]

    def test_register_with_pr(self, tmp_data_dir):
        register_bubble("test-pr", "org/repo", pr=42)
        info = get_bubble_info("test-pr")
        assert info["pr"] == 42


class TestCheckGitSynced:
    def test_clean_repo(self, tmp_data_dir, mock_runtime):
        mock_runtime.exec_responses["git status --porcelain"] = ""
        mock_runtime.exec_responses["git log --branches"] = ""
        synced, reason = check_git_synced(mock_runtime, "test", "/home/lean/repo")
        assert synced is True
        assert reason == ""

    def test_uncommitted_changes(self, tmp_data_dir, mock_runtime):
        mock_runtime.exec_responses["git status --porcelain"] = "M file.txt\n"
        synced, reason = check_git_synced(mock_runtime, "test", "/home/lean/repo")
        assert synced is False
        assert "Uncommitted" in reason

    def test_unpushed_commits(self, tmp_data_dir, mock_runtime):
        mock_runtime.exec_responses["git status --porcelain"] = ""
        mock_runtime.exec_responses["git log --branches"] = "abc1234 some commit\n"
        synced, reason = check_git_synced(mock_runtime, "test", "/home/lean/repo")
        assert synced is False
        assert "Unpushed" in reason


class TestArchive:
    def test_archive_updates_registry(self, tmp_data_dir, mock_runtime):
        register_bubble("test-archive", "org/repo", branch="main")
        mock_runtime.exec_responses["git branch --show-current"] = "main\n"
        mock_runtime.exec_responses["git rev-parse HEAD"] = "abc123\n"
        mock_runtime.exec_responses["lean-toolchain"] = "leanprover/lean4:v4.5.0\n"

        state = archive_bubble(mock_runtime, "test-archive", "/home/lean/repo")
        assert state["state"] == "archived"
        assert state["branch"] == "main"
        assert "archived_at" in state

    def test_archive_calls_delete(self, tmp_data_dir, mock_runtime):
        register_bubble("test-del", "org/repo")
        archive_bubble(mock_runtime, "test-del", "/home/lean/repo")
        delete_calls = [c for c in mock_runtime.calls if c[0] == "delete"]
        assert len(delete_calls) == 1
        assert delete_calls[0][1] == "test-del"
