"""Tests for GitHub token injection."""

from unittest.mock import MagicMock, patch

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


def test_setup_gh_token_local_with_owner_repo(mock_runtime):
    """Local container with owner/repo uses auth proxy (fail-closed)."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.setup_auth_proxy", return_value=True) as mock_proxy:
        result = setup_gh_token(mock_runtime, "test-container", owner="kim-em", repo="bubble")
        assert result is True
        mock_proxy.assert_called_once_with(
            mock_runtime,
            "test-container",
            "kim-em",
            "bubble",
            False,
            gh_enabled=False,
            config=None,
        )


def test_setup_gh_token_local_no_owner_repo(mock_runtime):
    """Local container without owner/repo returns False (fail-closed)."""
    from bubble.github_token import setup_gh_token

    result = setup_gh_token(mock_runtime, "test-container")
    assert result is False
    assert len(mock_runtime.calls) == 0


def test_setup_gh_token_remote_no_owner_repo():
    """Remote container without owner/repo returns False (fail-closed)."""
    from bubble.github_token import setup_gh_token

    result = setup_gh_token(None, "test-container", remote_host="fake-host")
    assert result is False


def test_setup_gh_token_remote_calls_proxy_remote():
    """Remote container with owner/repo uses tunneled auth proxy."""
    from bubble.github_token import setup_gh_token

    remote_host = MagicMock()
    with patch(
        "bubble.github_token.setup_auth_proxy_remote", return_value=True
    ) as mock_proxy_remote:
        result = setup_gh_token(
            None, "test-container", owner="kim-em", repo="bubble", remote_host=remote_host
        )
        assert result is True
        mock_proxy_remote.assert_called_once_with(
            remote_host,
            "test-container",
            "kim-em",
            "bubble",
            False,
            gh_enabled=False,
            config=None,
        )


def test_setup_auth_proxy_remote_starts_tunnel():
    """setup_auth_proxy_remote starts a tunnel and configures the container."""
    from bubble.auth_proxy import LEVEL_GIT_ONLY
    from bubble.github_token import setup_auth_proxy_remote

    remote_host = MagicMock()
    remote_host.spec_string.return_value = "myhost"

    with (
        patch("bubble.github_token._ensure_auth_proxy_running", return_value=7654),
        patch("bubble.tunnel.start_tunnel", return_value=True) as mock_tunnel,
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok123") as mock_gen,
        patch("bubble.remote._ssh_run") as mock_ssh,
    ):
        result = setup_auth_proxy_remote(remote_host, "my-container", "kim-em", "bubble")
        assert result is True

        # Tunnel started with local port
        mock_tunnel.assert_called_once_with(remote_host, local_port=7654)

        # Token generated for the container with access level
        mock_gen.assert_called_once_with("my-container", "kim-em", "bubble", level=LEVEL_GIT_ONLY)

        # Two SSH calls: incus device add + incus exec (git config)
        assert mock_ssh.call_count == 2
        device_call = mock_ssh.call_args_list[0]
        assert "bubble-auth-proxy" in device_call[0][1]
        git_call = mock_ssh.call_args_list[1]
        assert "git config" in " ".join(str(a) for a in git_call[0][1])


def test_setup_auth_proxy_remote_tunnel_fails():
    """setup_auth_proxy_remote returns False if tunnel fails."""
    from bubble.github_token import setup_auth_proxy_remote

    remote_host = MagicMock()

    with (
        patch("bubble.github_token._ensure_auth_proxy_running", return_value=7654),
        patch("bubble.tunnel.start_tunnel", return_value=False),
    ):
        result = setup_auth_proxy_remote(remote_host, "my-container", "kim-em", "bubble")
        assert result is False


def test_setup_auth_proxy_remote_proxy_not_running():
    """setup_auth_proxy_remote returns False if auth proxy isn't running."""
    from bubble.github_token import setup_auth_proxy_remote

    remote_host = MagicMock()

    with patch("bubble.github_token._ensure_auth_proxy_running", return_value=None):
        result = setup_auth_proxy_remote(remote_host, "my-container", "kim-em", "bubble")
        assert result is False


def test_setup_auth_proxy_remote_device_failure_cleans_token():
    """If Incus device add fails, the minted token is cleaned up."""
    from bubble.github_token import setup_auth_proxy_remote

    remote_host = MagicMock()

    with (
        patch("bubble.github_token._ensure_auth_proxy_running", return_value=7654),
        patch("bubble.tunnel.start_tunnel", return_value=True),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok123"),
        patch("bubble.remote._ssh_run", side_effect=RuntimeError("device add failed")),
        patch("bubble.auth_proxy.remove_auth_tokens") as mock_remove,
    ):
        result = setup_auth_proxy_remote(remote_host, "my-container", "kim-em", "bubble")
        assert result is False
        mock_remove.assert_called_once_with("my-container")


def test_setup_auth_proxy_remote_git_config_failure_cleans_token():
    """If git config fails, the minted token is cleaned up."""
    from bubble.github_token import setup_auth_proxy_remote

    remote_host = MagicMock()

    call_count = 0

    def ssh_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("git config failed")

    with (
        patch("bubble.github_token._ensure_auth_proxy_running", return_value=7654),
        patch("bubble.tunnel.start_tunnel", return_value=True),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok123"),
        patch("bubble.remote._ssh_run", side_effect=ssh_side_effect),
        patch("bubble.auth_proxy.remove_auth_tokens") as mock_remove,
    ):
        result = setup_auth_proxy_remote(remote_host, "my-container", "kim-em", "bubble")
        assert result is False
        mock_remove.assert_called_once_with("my-container")


# CLI tests


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


def test_gh_status_shows_security_setting(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = runner.invoke(main, ["gh", "status"])
    assert result.exit_code == 0
    assert "GitHub auth:" in result.output
    assert "effectively on" in result.output


# --- Token injection (level 5) tests ---


def test_inject_gh_token_local(mock_runtime):
    """inject_gh_token sets GH_TOKEN and GITHUB_TOKEN via profile.d."""
    from bubble.github_token import inject_gh_token

    with patch("bubble.github_token.get_host_gh_token", return_value="ghp_realtoken123"):
        result = inject_gh_token(mock_runtime, "test-container")
        assert result is True

    # Should have exec'd a bash command to write profile.d script
    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    cmd_str = " ".join(str(a) for a in exec_calls[0][2])
    assert "bubble-gh-inject.sh" in cmd_str
    assert "GH_TOKEN" in cmd_str
    assert "GITHUB_TOKEN" in cmd_str


def test_inject_gh_token_local_no_host_token(mock_runtime):
    """inject_gh_token returns False when host has no token."""
    from bubble.github_token import inject_gh_token

    with patch("bubble.github_token.get_host_gh_token", return_value=None):
        result = inject_gh_token(mock_runtime, "test-container")
        assert result is False
    assert len(mock_runtime.calls) == 0


def test_inject_gh_token_local_exec_failure(mock_runtime):
    """inject_gh_token returns False on exec failure."""
    from bubble.github_token import inject_gh_token

    # Make exec raise for this specific call
    def failing_exec(name, command, **kwargs):
        raise RuntimeError("exec failed")

    mock_runtime.exec = failing_exec

    with patch("bubble.github_token.get_host_gh_token", return_value="ghp_realtoken123"):
        result = inject_gh_token(mock_runtime, "test-container")
        assert result is False


def test_inject_gh_token_remote():
    """inject_gh_token_remote injects token via SSH."""
    from bubble.github_token import inject_gh_token_remote

    remote_host = MagicMock()

    with (
        patch("bubble.github_token.get_host_gh_token", return_value="ghp_realtoken123"),
        patch("bubble.remote._ssh_run") as mock_ssh,
    ):
        result = inject_gh_token_remote(remote_host, "test-container")
        assert result is True

        mock_ssh.assert_called_once()
        call_args = mock_ssh.call_args[0][1]
        cmd_str = " ".join(str(a) for a in call_args)
        assert "bubble-gh-inject.sh" in cmd_str
        assert "GH_TOKEN" in cmd_str


def test_inject_gh_token_remote_no_host_token():
    """inject_gh_token_remote returns False when host has no token."""
    from bubble.github_token import inject_gh_token_remote

    remote_host = MagicMock()

    with patch("bubble.github_token.get_host_gh_token", return_value=None):
        result = inject_gh_token_remote(remote_host, "test-container")
        assert result is False


def test_inject_gh_token_remote_ssh_failure():
    """inject_gh_token_remote returns False on SSH failure."""
    from bubble.github_token import inject_gh_token_remote

    remote_host = MagicMock()

    with (
        patch("bubble.github_token.get_host_gh_token", return_value="ghp_realtoken123"),
        patch("bubble.remote._ssh_run", side_effect=RuntimeError("ssh failed")),
    ):
        result = inject_gh_token_remote(remote_host, "test-container")
        assert result is False


def test_setup_gh_token_with_token_inject_local(mock_runtime):
    """setup_gh_token with token_inject=True uses inject_gh_token."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.inject_gh_token", return_value=True) as mock_inject:
        result = setup_gh_token(mock_runtime, "test-container", token_inject=True)
        assert result is True
        mock_inject.assert_called_once_with(mock_runtime, "test-container", False)


def test_setup_gh_token_with_token_inject_remote():
    """setup_gh_token with token_inject=True and remote_host uses inject_gh_token_remote."""
    from bubble.github_token import setup_gh_token

    remote_host = MagicMock()
    with patch("bubble.github_token.inject_gh_token_remote", return_value=True) as mock_inject:
        result = setup_gh_token(None, "test-container", remote_host=remote_host, token_inject=True)
        assert result is True
        mock_inject.assert_called_once_with(remote_host, "test-container", False)


def test_setup_gh_token_with_token_inject_no_runtime():
    """setup_gh_token with token_inject=True but no runtime returns False."""
    from bubble.github_token import setup_gh_token

    result = setup_gh_token(None, "test-container", token_inject=True)
    assert result is False


def test_setup_gh_token_with_token_inject_skips_owner_repo_check(mock_runtime):
    """Token injection doesn't require owner/repo (it gives full access anyway)."""
    from bubble.github_token import setup_gh_token

    with patch("bubble.github_token.inject_gh_token", return_value=True) as mock_inject:
        result = setup_gh_token(mock_runtime, "test-container", token_inject=True)
        assert result is True
        mock_inject.assert_called_once()


def test_gh_status_shows_token_injection(tmp_data_dir):
    """gh status shows token injection setting."""
    from bubble.cli import main

    runner = CliRunner()
    with patch("bubble.github_token.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = runner.invoke(main, ["gh", "status"])
    assert result.exit_code == 0
    assert "Token injection:" in result.output
    assert "effectively off" in result.output
