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


def test_explicit_open_with_branch_not_doubled():
    """When 'open' is already in args, -b should not cause 'open' to be prepended again.

    Regression test for https://github.com/kim-em/bubble/issues/259:
    `bubble --ssh chonk -b branch leanprover/lean4` failed on the remote side
    because parse_args prepended 'open' even when 'open' was already present,
    resulting in 'open' being treated as a target.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["open", "-b", "my-branch", "target1"])
    # Should NOT produce the multi-target error (only one target)
    assert "--new-branch cannot be used with multiple targets" not in (result.output or "")


def test_branch_value_matching_command_name():
    """A -b value that matches a command name (e.g. 'list') should not hijack routing.

    Regression test: `bubble -b list target1` should route to `open`, not `list`.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["-b", "list", "target1"])
    # The `list` command doesn't have -b, so if routing went wrong we'd see
    # "No such option: -b". Instead we should get routed to `open`.
    assert "No such option" not in (result.output or "")
