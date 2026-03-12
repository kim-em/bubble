"""Tests for lifecycle management (registry)."""

import json

from bubble.lifecycle import (
    get_bubble_info,
    prune_stale_entries,
    register_bubble,
    unregister_bubble,
)


class TestRegistry:
    def test_register_and_get(self, tmp_data_dir):
        register_bubble("test-bubble", "org/repo", branch="main", commit="abc123")
        info = get_bubble_info("test-bubble")
        assert info is not None
        assert info["org_repo"] == "org/repo"
        assert info["branch"] == "main"
        assert info["commit"] == "abc123"

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

    def test_unregister(self, tmp_data_dir):
        register_bubble("to-remove", "org/repo")
        assert get_bubble_info("to-remove") is not None
        unregister_bubble("to-remove")
        assert get_bubble_info("to-remove") is None

    def test_unregister_nonexistent(self, tmp_data_dir):
        # Should not raise
        unregister_bubble("nonexistent")

    def test_register_with_remote_host(self, tmp_data_dir):
        register_bubble("remote-bubble", "org/repo", remote_host="kim@server:2222")
        info = get_bubble_info("remote-bubble")
        assert info is not None
        assert info["remote_host"] == "kim@server:2222"

    def test_register_without_remote_host(self, tmp_data_dir):
        register_bubble("local-bubble", "org/repo")
        info = get_bubble_info("local-bubble")
        assert info is not None
        assert "remote_host" not in info

    def test_register_with_project_dir(self, tmp_data_dir):
        register_bubble("dir-bubble", "org/repo", project_dir="/home/user/myrepo")
        info = get_bubble_info("dir-bubble")
        assert info is not None
        assert info["project_dir"] == "/home/user/myrepo"

    def test_register_without_project_dir(self, tmp_data_dir):
        register_bubble("no-dir-bubble", "org/repo")
        info = get_bubble_info("no-dir-bubble")
        assert info is not None
        assert "project_dir" not in info


class TestPruneStaleEntries:
    def test_prunes_missing_local_containers(self, tmp_data_dir):
        register_bubble("alive", "org/repo")
        register_bubble("dead", "org/repo")
        pruned = prune_stale_entries({"alive"})
        assert pruned == ["dead"]
        assert get_bubble_info("alive") is not None
        assert get_bubble_info("dead") is None

    def test_preserves_remote_entries(self, tmp_data_dir):
        register_bubble("remote-one", "org/repo", remote_host="user@host")
        pruned = prune_stale_entries(set())
        assert pruned == []
        assert get_bubble_info("remote-one") is not None

    def test_preserves_native_entries(self, tmp_data_dir):
        register_bubble("native-one", "org/repo", native=True, native_path="/tmp/x")
        pruned = prune_stale_entries(set())
        assert pruned == []
        assert get_bubble_info("native-one") is not None

    def test_no_stale_entries(self, tmp_data_dir):
        register_bubble("a", "org/repo")
        register_bubble("b", "org/repo")
        pruned = prune_stale_entries({"a", "b", "extra"})
        assert pruned == []
        assert get_bubble_info("a") is not None
        assert get_bubble_info("b") is not None

    def test_empty_registry(self, tmp_data_dir):
        pruned = prune_stale_entries({"something"})
        assert pruned == []
