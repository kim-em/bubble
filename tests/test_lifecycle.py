"""Tests for lifecycle management (registry)."""

import json

from bubble.lifecycle import (
    get_bubble_info,
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
