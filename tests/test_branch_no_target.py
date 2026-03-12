"""Tests for `bubble -b branch_name` without an explicit target."""

import os
import subprocess
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bubble.cli import main
from bubble.target import Target


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with a GitHub remote."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:leanprover-community/mathlib4.git",
        ],
        capture_output=True,
        check=True,
    )
    return tmp_path


def _apply_open_patches(stack, parse_side_effect=None):
    """Apply patches that short-circuit _open_single and return the get_runtime mock."""
    stack.enter_context(patch("bubble.cli.load_config", return_value={}))
    stack.enter_context(patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com")))
    rt_mock = stack.enter_context(patch("bubble.cli.get_runtime"))
    rt_mock.return_value.list_containers.return_value = []
    stack.enter_context(patch("bubble.cli.find_existing_container", return_value=None))
    stack.enter_context(patch("bubble.cli.print_warnings"))
    stack.enter_context(patch("bubble.cli.maybe_rebuild_base_image"))
    stack.enter_context(patch("bubble.cli.maybe_rebuild_tools"))
    stack.enter_context(patch("bubble.cli.maybe_rebuild_customize"))
    stack.enter_context(patch("bubble.cli.maybe_symlink_claude_projects"))
    stack.enter_context(patch("bubble.cli.RepoRegistry"))
    stack.enter_context(patch("bubble.cli.parse_target", side_effect=parse_side_effect))
    # Stop execution right after parse_target succeeds
    stack.enter_context(patch("bubble.cli._resolve_ref_source", side_effect=SystemExit(0)))
    return rt_mock


class TestBranchWithoutTarget:
    def test_b_without_target_infers_cwd(self, git_repo):
        """bubble -b my_branch without a target should resolve owner/repo from cwd."""
        captured = []

        def capture(target, registry):
            captured.append(target)
            return Target(
                owner="leanprover-community",
                repo="mathlib4",
                kind="repo",
                ref="",
                original=target,
            )

        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(git_repo)
            with ExitStack() as stack:
                _apply_open_patches(stack, parse_side_effect=capture)
                runner.invoke(main, ["-b", "my-new-branch"])
        finally:
            os.chdir(old_cwd)

        # Should resolve to owner/repo (not ".") so it works for remote flows
        assert captured == ["leanprover-community/mathlib4"], (
            f"Expected parse_target called with 'leanprover-community/mathlib4', got {captured}"
        )

    def test_open_no_target_without_b_errors(self):
        """bubble open with no target and no -b should give a clear error."""
        runner = CliRunner()
        result = runner.invoke(main, ["open"])
        assert result.exit_code != 0
        assert "missing target" in result.output.lower()

    def test_b_without_target_not_in_git_repo(self, tmp_path):
        """bubble -b my_branch outside a git repo should error (can't infer repo)."""
        runner = CliRunner()
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(main, ["-b", "my-new-branch"])
        finally:
            os.chdir(old_cwd)
        assert result.exit_code != 0

    def test_b_with_explicit_target_still_works(self):
        """bubble -b my_branch mathlib4 should still work (backward compat)."""
        captured = []

        def capture(target, registry):
            captured.append(target)
            return Target(
                owner="leanprover-community",
                repo="mathlib4",
                kind="repo",
                ref="",
                original="mathlib4",
            )

        runner = CliRunner()
        with ExitStack() as stack:
            _apply_open_patches(stack, parse_side_effect=capture)
            runner.invoke(main, ["-b", "my-new-branch", "mathlib4"])

        assert captured == ["mathlib4"], (
            f"Expected parse_target called with 'mathlib4', got {captured}"
        )

    def test_help_not_routed_to_open(self):
        """bubble --help should show group help, not open subcommand help."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "bubble TARGET" in result.output

    def test_help_shows_common_target_options(self):
        """bubble --help should surface common target options and a hint."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Common target options:" in result.output
        assert "--shell" in result.output
        assert "--cloud" in result.output
        assert "-b, --new-branch" in result.output
        assert "bubble open --help" in result.output

    def test_version_not_routed_to_open(self):
        """bubble --version should work, not be routed to open."""
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
