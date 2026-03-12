"""Tests for the claude-projects symlink feature."""

from unittest.mock import patch

import pytest

from bubble.config import do_symlink_claude_projects, maybe_symlink_claude_projects


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

    def test_prints_hint_when_real_dir_exists(self, setup_dirs, capsys):
        """Prints informational message when ~/.bubble/claude-projects/ is a real dir."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            maybe_symlink_claude_projects()

        # Directory is NOT replaced (no prompt, no auto-replace)
        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()

        # Message was printed to stderr
        captured = capsys.readouterr()
        assert "symlink-claude-projects" in captured.err
        assert "claude_projects_symlink" in captured.err

    def test_suppressed_by_config(self, setup_dirs, capsys):
        """No message when claude_projects_symlink = 'no' in config."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            maybe_symlink_claude_projects(config={"claude_projects_symlink": "no"})

        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_handles_empty_bubble_projects_dir(self, setup_dirs, capsys):
        """Prints hint when ~/.bubble/claude-projects/ exists but is empty."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            maybe_symlink_claude_projects()

        # Not replaced — just a hint
        assert bubble_projects.is_dir()
        assert not bubble_projects.is_symlink()
        assert "symlink-claude-projects" in capsys.readouterr().err


class TestDoSymlinkClaudeProjects:
    def test_replaces_dir_with_symlink(self, setup_dirs):
        """Replaces ~/.bubble/claude-projects/ with a symlink and merges contents."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        # Add some content to bubble_projects
        (bubble_projects / "session-a").mkdir()
        (bubble_projects / "session-a" / "data.jsonl").write_text("data")

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is True
        assert bubble_projects.is_symlink()
        assert bubble_projects.resolve() == claude_projects.resolve()
        # Content was moved
        assert (claude_projects / "session-a" / "data.jsonl").read_text() == "data"

    def test_creates_symlink_when_bubble_dir_missing(self, setup_dirs):
        """Creates symlink directly when ~/.bubble/claude-projects/ doesn't exist."""
        claude_projects, bubble_projects = setup_dirs

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is True
        assert bubble_projects.is_symlink()
        assert bubble_projects.resolve() == claude_projects.resolve()

    def test_noop_when_already_symlink(self, setup_dirs):
        """Returns True when already a symlink."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.symlink_to(claude_projects)

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is True
        assert bubble_projects.is_symlink()

    def test_fails_when_not_git_tracked(self, setup_dirs):
        """Returns False when ~/.claude/projects/ is not in a git repo."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        with patch("bubble.config._is_inside_git_repo", return_value=False):
            result = do_symlink_claude_projects()

        assert result is False
        assert not bubble_projects.is_symlink()

    def test_merges_non_conflicting_contents(self, setup_dirs):
        """Merges successfully when there are no file conflicts."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        # Same directory name in both, with different files inside (no conflict)
        (claude_projects / "shared").mkdir()
        (claude_projects / "shared" / "original.txt").write_text("original")
        (bubble_projects / "shared").mkdir()
        (bubble_projects / "shared" / "different.txt").write_text("bubble-data")

        # Unique to bubble
        (bubble_projects / "unique").mkdir()
        (bubble_projects / "unique" / "data.txt").write_text("unique-data")

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is True
        assert bubble_projects.is_symlink()
        # Original was preserved (not overwritten)
        assert (claude_projects / "shared" / "original.txt").read_text() == "original"
        # Bubble-only file inside shared dir was merged in
        assert (claude_projects / "shared" / "different.txt").read_text() == "bubble-data"
        # Unique content was moved
        assert (claude_projects / "unique" / "data.txt").read_text() == "unique-data"

    def test_aborts_on_file_conflicts_nothing_moved(self, setup_dirs, capsys):
        """Aborts without moving anything when conflicts exist."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        # Create a file conflict: same filename in both locations
        (claude_projects / "conflict.txt").write_text("claude-version")
        (bubble_projects / "conflict.txt").write_text("bubble-version")

        # Also a non-conflicting file — must NOT be moved on abort
        (bubble_projects / "safe.txt").write_text("safe-data")

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is False
        # Directory was NOT replaced with symlink
        assert not bubble_projects.is_symlink()
        assert bubble_projects.is_dir()
        # Conflicting file in bubble was preserved
        assert (bubble_projects / "conflict.txt").read_text() == "bubble-version"
        # Claude version was not overwritten
        assert (claude_projects / "conflict.txt").read_text() == "claude-version"
        # Non-conflicting file was NOT moved (atomic: nothing moves on conflict)
        assert (bubble_projects / "safe.txt").read_text() == "safe-data"
        assert not (claude_projects / "safe.txt").exists()
        # Error message was printed
        captured = capsys.readouterr()
        assert "Aborted" in captured.err
        assert "1 file(s)" in captured.err

    def test_aborts_on_nested_file_conflicts_nothing_moved(self, setup_dirs, capsys):
        """Aborts without moving anything when nested conflicts exist."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        # Nested conflict
        (claude_projects / "shared").mkdir()
        (claude_projects / "shared" / "same.txt").write_text("claude")
        (bubble_projects / "shared").mkdir()
        (bubble_projects / "shared" / "same.txt").write_text("bubble")

        # Non-conflicting sibling — must NOT be moved
        (bubble_projects / "other.txt").write_text("other-data")

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is False
        assert not bubble_projects.is_symlink()
        # Nothing was moved
        assert (bubble_projects / "other.txt").read_text() == "other-data"
        assert not (claude_projects / "other.txt").exists()
        captured = capsys.readouterr()
        assert "Aborted" in captured.err

    def test_fails_when_bubble_projects_is_file(self, setup_dirs):
        """Returns False when ~/.bubble/claude-projects is a file, not a dir."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.write_text("not a directory")

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is False
        assert not bubble_projects.is_symlink()

    def test_fails_when_symlink_points_elsewhere(self, setup_dirs):
        """Returns False when existing symlink points to wrong target."""
        claude_projects, bubble_projects = setup_dirs
        other_dir = bubble_projects.parent / "other"
        other_dir.mkdir()
        bubble_projects.symlink_to(other_dir)

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is False

    def test_succeeds_when_symlink_points_to_claude_projects(self, setup_dirs):
        """Returns True when existing symlink already points to correct target."""
        claude_projects, bubble_projects = setup_dirs
        bubble_projects.symlink_to(claude_projects)

        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = do_symlink_claude_projects()

        assert result is True


class TestSymlinkClaudeProjectsCLI:
    def test_exit_code_on_failure(self, setup_dirs):
        """CLI command exits with code 1 on failure."""
        from click.testing import CliRunner

        from bubble.cli import main

        claude_projects, bubble_projects = setup_dirs
        bubble_projects.mkdir()

        runner = CliRunner()
        with patch("bubble.config._is_inside_git_repo", return_value=False):
            result = runner.invoke(main, ["config", "symlink-claude-projects"])

        assert result.exit_code != 0

    def test_exit_code_on_success(self, setup_dirs):
        """CLI command exits with code 0 on success."""
        from click.testing import CliRunner

        from bubble.cli import main

        claude_projects, bubble_projects = setup_dirs
        # Don't create bubble_projects — simplest success path

        runner = CliRunner()
        with patch("bubble.config._is_inside_git_repo", return_value=True):
            result = runner.invoke(main, ["config", "symlink-claude-projects"])

        assert result.exit_code == 0
        assert bubble_projects.is_symlink()


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
