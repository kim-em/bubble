"""Tests for multi-target support in the open command."""

from click.testing import CliRunner

from bubble.cli import main


def test_multi_target_rejects_name():
    """--name cannot be used with multiple targets."""
    runner = CliRunner()
    result = runner.invoke(main, ["open", "--name", "foo", "target1", "target2"])
    assert result.exit_code != 0
    assert "--name cannot be used with multiple targets" in result.output


def test_multi_target_rejects_new_branch():
    """-b/--new-branch cannot be used with multiple targets."""
    runner = CliRunner()
    result = runner.invoke(main, ["-b", "my-branch", "target1", "target2"])
    assert result.exit_code != 0
    assert "--new-branch cannot be used with multiple targets" in result.output


def test_single_target_allows_name():
    """--name with a single target should not produce the multi-target error."""
    runner = CliRunner()
    result = runner.invoke(main, ["open", "--name", "foo", "target1"])
    # It may fail for other reasons (no runtime), but NOT the multi-target error
    assert "--name cannot be used with multiple targets" not in (result.output or "")
