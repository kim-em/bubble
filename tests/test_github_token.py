"""Tests for GitHub token injection."""

from unittest.mock import patch

from click.testing import CliRunner

from bubble.github_token import get_host_gh_token, has_gh_auth


def test_get_host_gh_token_success():
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "gho_abc123\n"
        token = get_host_gh_token()
        assert token == "gho_abc123"


def test_get_host_gh_token_not_authed():
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        token = get_host_gh_token()
        assert token is None


def test_get_host_gh_token_gh_not_installed():
    with patch("bubble.github_token.subprocess.run", side_effect=FileNotFoundError):
        token = get_host_gh_token()
        assert token is None


def test_has_gh_auth_true():
    """has_gh_auth uses gh auth status, not the actual token."""
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        assert has_gh_auth() is True
        # Verify it called gh auth status, not gh auth token
        args = mock_run.call_args[0][0]
        assert args == ["gh", "auth", "status"]


def test_has_gh_auth_false():
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        assert has_gh_auth() is False


def test_has_gh_auth_gh_not_installed():
    with patch("bubble.github_token.subprocess.run", side_effect=FileNotFoundError):
        assert has_gh_auth() is False


def test_inject_gh_token_uses_push_file(mock_runtime):
    """Verify inject_gh_token uses push_file (not argv) to transfer the token."""
    from bubble.github_token import inject_gh_token

    result = inject_gh_token(mock_runtime, "test-container", "gho_abc123")
    assert result is True

    # Token should be pushed via file, not embedded in exec args
    push_calls = [c for c in mock_runtime.calls if c[0] == "push_file"]
    assert len(push_calls) == 1
    assert push_calls[0][1] == "test-container"
    assert push_calls[0][3] == "/tmp/.gh-token"

    # The exec command should reference the file, not contain the token
    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    cmd_str = " ".join(exec_calls[0][2])
    assert "gh auth login --with-token" in cmd_str
    assert "gh auth setup-git" in cmd_str
    # Token should NOT appear in the exec command
    assert "gho_abc123" not in cmd_str


def test_inject_gh_token_cleans_up_host_temp_file(mock_runtime, tmp_path):
    """Verify the host-side temp file is deleted after push."""
    from bubble.github_token import inject_gh_token

    inject_gh_token(mock_runtime, "test-container", "gho_abc123")

    # The push_file call's source path should no longer exist
    push_calls = [c for c in mock_runtime.calls if c[0] == "push_file"]
    import os

    assert not os.path.exists(push_calls[0][2])


def test_inject_gh_token_failure_returns_false(mock_runtime):
    """Verify inject_gh_token returns False on exec failure."""
    from bubble.github_token import inject_gh_token

    # Make exec raise RuntimeError
    mock_runtime.exec_responses["__raise__"] = True

    class FailingRuntime:
        """Minimal runtime that fails on exec but records push_file calls."""

        def __init__(self):
            self.calls = []

        def push_file(self, name, local_path, remote_path):
            self.calls.append(("push_file", name, local_path, remote_path))

        def exec(self, name, command, **kwargs):
            raise RuntimeError("exec failed")

    runtime = FailingRuntime()
    result = inject_gh_token(runtime, "test-container", "gho_abc123")
    assert result is False


def test_setup_gh_token_success(mock_runtime):
    """Verify setup_gh_token gets host token and injects it."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.get_host_gh_token", return_value="gho_abc123"):
        result = setup_gh_token(mock_runtime, "test-container")
        assert result is True

    push_calls = [c for c in mock_runtime.calls if c[0] == "push_file"]
    assert len(push_calls) == 1


def test_setup_gh_token_no_host_auth(mock_runtime):
    """Verify setup_gh_token returns False when host has no auth."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.get_host_gh_token", return_value=None):
        result = setup_gh_token(mock_runtime, "test-container")
        assert result is False

    assert len(mock_runtime.calls) == 0


# CLI tests


def test_gh_token_on_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["gh", "token", "on"])
    assert result.exit_code == 0
    assert "enabled" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["github"]["token"] is True


def test_gh_token_off_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["gh", "token", "off"])
    assert result.exit_code == 0
    assert "disabled" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["github"]["token"] is False


def test_gh_status_cli_not_authed(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        result = runner.invoke(main, ["gh", "status"])
    assert result.exit_code == 0
    assert "not authenticated" in result.output


def test_gh_status_cli_authed(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = runner.invoke(main, ["gh", "status"])
    assert result.exit_code == 0
    assert "authenticated" in result.output


def test_gh_token_config_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["github"] = {"token": True}
    save_config(config)

    reloaded = load_config()
    assert reloaded["github"]["token"] is True
