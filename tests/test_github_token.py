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
    with patch("bubble.github_token.get_host_gh_token", return_value="gho_abc123"):
        assert has_gh_auth() is True


def test_has_gh_auth_false():
    with patch("bubble.github_token.get_host_gh_token", return_value=None):
        assert has_gh_auth() is False


def test_inject_gh_token_calls_runtime(mock_runtime):
    """Verify inject_gh_token runs gh auth login inside the container."""
    from bubble.github_token import inject_gh_token

    inject_gh_token(mock_runtime, "test-container", "gho_abc123")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    cmd_str = " ".join(exec_calls[0][2])
    assert "gh auth login --with-token" in cmd_str
    assert "gh auth setup-git" in cmd_str


def test_setup_gh_token_success(mock_runtime):
    """Verify setup_gh_token gets host token and injects it."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.get_host_gh_token", return_value="gho_abc123"):
        result = setup_gh_token(mock_runtime, "test-container")
        assert result is True

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) == 1


def test_setup_gh_token_no_host_auth(mock_runtime):
    """Verify setup_gh_token returns False when host has no auth."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.get_host_gh_token", return_value=None):
        result = setup_gh_token(mock_runtime, "test-container")
        assert result is False

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) == 0


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
    with patch("bubble.github_token.get_host_gh_token", return_value=None):
        result = runner.invoke(main, ["gh", "status"])
    assert result.exit_code == 0
    assert "not authenticated" in result.output


def test_gh_status_cli_authed(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    with patch("bubble.github_token.get_host_gh_token", return_value="gho_abc123"):
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
