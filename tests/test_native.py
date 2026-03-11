"""Tests for native (non-containerized) workspace support."""

import json
import subprocess

from bubble.clean import check_native_clean
from bubble.lifecycle import get_bubble_info, register_bubble, unregister_bubble


class TestNativeRegistry:
    def test_register_native_bubble(self, tmp_data_dir):
        register_bubble(
            "test-native",
            "org/repo",
            branch="main",
            commit="abc123",
            native=True,
            native_path="/tmp/bubble/native/test-native",
        )
        info = get_bubble_info("test-native")
        assert info is not None
        assert info["native"] is True
        assert info["native_path"] == "/tmp/bubble/native/test-native"
        assert info["org_repo"] == "org/repo"
        assert info["branch"] == "main"

    def test_register_native_with_pr(self, tmp_data_dir):
        register_bubble(
            "test-native-pr",
            "org/repo",
            pr=42,
            native=True,
            native_path="/tmp/bubble/native/test-native-pr",
        )
        info = get_bubble_info("test-native-pr")
        assert info["pr"] == 42
        assert info["native"] is True

    def test_non_native_has_no_native_flag(self, tmp_data_dir):
        register_bubble("test-container", "org/repo")
        info = get_bubble_info("test-container")
        assert "native" not in info
        assert "native_path" not in info

    def test_unregister_native(self, tmp_data_dir):
        register_bubble(
            "to-remove",
            "org/repo",
            native=True,
            native_path="/tmp/test",
        )
        assert get_bubble_info("to-remove") is not None
        unregister_bubble("to-remove")
        assert get_bubble_info("to-remove") is None

    def test_registry_json_structure(self, tmp_data_dir):
        register_bubble(
            "native-1",
            "org/repo1",
            native=True,
            native_path="/tmp/native-1",
        )
        register_bubble("container-1", "org/repo2")

        import bubble.config as config

        content = config.REGISTRY_FILE.read_text()
        data = json.loads(content)
        assert "native-1" in data["bubbles"]
        assert data["bubbles"]["native-1"]["native"] is True
        assert "container-1" in data["bubbles"]
        assert "native" not in data["bubbles"]["container-1"]


class TestNativeClean:
    def test_missing_path(self, tmp_data_dir):
        register_bubble(
            "missing",
            "org/repo",
            native=True,
            native_path="/nonexistent/path",
        )
        cs = check_native_clean("/nonexistent/path", "missing")
        assert not cs.clean
        assert cs.error == "path not found"

    def test_not_a_git_repo(self, tmp_path, tmp_data_dir):
        workspace = tmp_path / "not-git"
        workspace.mkdir()
        register_bubble(
            "no-git",
            "org/repo",
            native=True,
            native_path=str(workspace),
        )
        cs = check_native_clean(str(workspace), "no-git")
        assert not cs.clean
        assert cs.error == "not a git repo"

    def test_clean_repo(self, tmp_path, tmp_data_dir):
        workspace = tmp_path / "clean-repo"
        workspace.mkdir()
        try:
            subprocess.run(
                ["git", "init", str(workspace)],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            import pytest

            pytest.skip("git not available")
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        # Create an initial commit so HEAD exists
        (workspace / "file.txt").write_text("hello")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "file.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        commit = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        register_bubble(
            "clean-test",
            "org/repo",
            commit=commit,
            native=True,
            native_path=str(workspace),
        )
        cs = check_native_clean(str(workspace), "clean-test")
        assert cs.clean
        assert cs.reasons == []

    def test_dirty_repo(self, tmp_path, tmp_data_dir):
        workspace = tmp_path / "dirty-repo"
        workspace.mkdir()
        try:
            subprocess.run(
                ["git", "init", str(workspace)],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            import pytest

            pytest.skip("git not available")
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        (workspace / "file.txt").write_text("hello")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "file.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )
        # Make it dirty
        (workspace / "file.txt").write_text("modified")

        register_bubble(
            "dirty-test",
            "org/repo",
            native=True,
            native_path=str(workspace),
        )
        cs = check_native_clean(str(workspace), "dirty-test")
        assert not cs.clean
        assert "dirty_worktree" in cs.reasons
