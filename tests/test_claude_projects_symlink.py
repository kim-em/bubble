"""Tests for the claude-projects symlink feature."""

from unittest.mock import patch

import pytest

from bubble.config import maybe_symlink_claude_projects


@pytest.fixture
def setup_dirs(tmp_path, monkeypatch):
    """Set up temporary claude and bubble directories."""
    import bubble.config as config

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    claude_projects = claude_dir / "projects"
    claude_projects.mkdir()

    bubble_dir = tmp_path / ".bubble"
    bubble_dir.mkdir()
    bubble_projects = bubble_dir / "claude-projects"

    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIR", claude_dir)
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", bubble_projects)
    monkeypatch.setattr(config, "DATA_DIR", bubble_dir)

    return claude_projects, bubble_projects


class TestMaybeSymlinkClaudeProjects:
    def test_noop_when_claude_projects_not_in_git_repo(self, setup_dirs):
        """No action when ~/.claude/projects/ is not in a git repo."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        with patch("bubble.config._is_inside_git_repo", return_value=False):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()

    def test_noop_when_claude_projects_missing(self, tmp_path, monkeypatch):
        """No action when ~/.claude/projects/ doesn't exist."""
        import bubble.config as config

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # Don't create projects/ subdirectory

        bubble_projects = tmp_path / ".bubble" / "claude-projects"
        bubble_projects.mkdir(parents=True)

        monkeypatch.setattr(config, "CLAUDE_CONFIG_DIR", claude_dir)
        monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", bubble_projects)

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()

    def test_noop_when_already_symlink(self, setup_dirs):
        """No action when ~/.bubble/claude-projects/ is already a symlink."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.symlink_to(claude_projects)

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_symlink()
        assert bubble_projects.resolve() == claude_projects.resolve()

    def test_creates_symlink_when_bubble_dir_missing(self, setup_dirs):
        """Creates symlink directly when ~/.bubble/claude-projects/ doesn't exist."""
        claude_projects, bubble_projects = setup_dirs
        # Don't create bubble_projects

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_symlink()
        assert bubble_projects.resolve() == claude_projects.resolve()

    def test_prompts_and_replaces_on_accept(self, setup_dirs):
        """Prompts user and creates symlink when accepted."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        # Add some content to bubble_projects
        (bubble_projects / "session-a").mkdir()
        (bubble_projects / "session-a" / "data.jsonl").write_text("data")

        with (
            patch("bubble.config._is_inside_git_repo", return_value=True),
            patch("sys.stdin", **{"isatty.return_value": True}),
            patch("click.confirm", return_value=True),
        ):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_symlink()
        assert bubble_projects.resolve() == claude_projects.resolve()
        # Content was moved
        assert (claude_projects / "session-a" / "data.jsonl").read_text() == "data"

    def test_prompts_and_skips_on_deny(self, setup_dirs):
        """No changes when user denies the prompt."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()
        (bubble_projects / "keep-me").write_text("data")

        with (
            patch("bubble.config._is_inside_git_repo", return_value=True),
            patch("sys.stdin", **{"isatty.return_value": True}),
            patch("click.confirm", return_value=False),
        ):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()
        assert (bubble_projects / "keep-me").read_text() == "data"

    def test_noop_when_not_tty(self, setup_dirs):
        """No prompt when stdin is not a TTY."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()
        (bubble_projects / "keep-me").write_text("data")

        with (
            patch("bubble.config._is_inside_git_repo", return_value=True),
            patch("sys.stdin", **{"isatty.return_value": False}),
        ):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()

    def test_merges_without_overwriting_conflicts(self, setup_dirs):
        """Existing files are not overwritten; bubble-only files are preserved."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        # Same directory name in both, with different files inside
        (claude_projects / "shared").mkdir()
        (claude_projects / "shared" / "original.txt").write_text("original")
        (bubble_projects / "shared").mkdir()
        (bubble_projects / "shared" / "different.txt").write_text("bubble-data")

        # Unique to bubble
        (bubble_projects / "unique").mkdir()
        (bubble_projects / "unique" / "data.txt").write_text("unique-data")

        with (
            patch("bubble.config._is_inside_git_repo", return_value=True),
            patch("sys.stdin", **{"isatty.return_value": True}),
            patch("click.confirm", return_value=True),
        ):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_symlink()
        # Original was preserved (not overwritten)
        assert (claude_projects / "shared" / "original.txt").read_text() == "original"
        # Bubble-only file inside conflicting dir was merged in
        assert (claude_projects / "shared" / "different.txt").read_text() == "bubble-data"
        # Unique content was moved
        assert (claude_projects / "unique" / "data.txt").read_text() == "unique-data"

    def test_handles_empty_bubble_projects_dir(self, setup_dirs):
        """Works when ~/.bubble/claude-projects/ exists but is empty."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        with (
            patch("bubble.config._is_inside_git_repo", return_value=True),
            patch("sys.stdin", **{"isatty.return_value": True}),
            patch("click.confirm", return_value=True),
        ):
            maybe_symlink_claude_projects()

        assert bubble_projects.is_symlink()
        assert bubble_projects.resolve() == claude_projects.resolve()


class TestIsInsideGitRepo:
    def test_inside_git_repo(self, tmp_path):
        """Returns True for a directory inside a git repo."""
        from bubble.config import _is_inside_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess

        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)

        subdir = repo / "subdir"
        subdir.mkdir()

        assert _is_inside_git_repo(subdir) is True

    def test_not_inside_git_repo(self, tmp_path):
        """Returns False for a directory not inside a git repo."""
        from bubble.config import _is_inside_git_repo

        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()

        assert _is_inside_git_repo(plain_dir) is False

    def test_nonexistent_directory(self, tmp_path):
        """Returns False for a directory that doesn't exist."""
        from bubble.config import _is_inside_git_repo

        assert _is_inside_git_repo(tmp_path / "nope") is False
