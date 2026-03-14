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

        # Token generated for the container with rest_api and graphql policies
        mock_gen.assert_called_once_with(
            "my-container",
            "kim-em",
            "bubble",
            rest_api=False,
            graphql_read="none",
            graphql_write="none",
        )

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
    assert "GitHub level:" in result.output
    assert "allowlist-write-graphql" in result.output


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


# --- Ordering test: auth proxy must be set up before clone (issue #221) ---


def test_auth_proxy_setup_before_clone(mock_runtime, tmp_data_dir, tmp_ssh_dir):
    """Auth proxy setup must run between provision and clone (fixes #221).

    When network allowlisting is enabled, github.com is stripped from allowed
    domains (traffic goes through the loopback auth proxy instead). If git
    url.insteadOf isn't configured before clone, git clone fails because
    github.com is blocked by iptables.
    """
    from bubble.target import Target

    call_order = []

    t = Target(
        owner="kim-em",
        repo="bubble",
        kind="main",
        ref="",
        original="kim-em/bubble",
    )

    def mock_provision(*args, **kwargs):
        call_order.append("provision")

    call_order_kwargs = {}

    def mock_setup_gh_token(*args, **kwargs):
        call_order.append("setup_gh_token")
        call_order_kwargs.update(kwargs)
        return True

    def mock_clone(*args, **kwargs):
        call_order.append("clone")
        return ""

    def mock_finalize(*args, **kwargs):
        call_order.append("finalize")

    # Return "allowlist-write-graphql" as the github level, matching the
    # default security posture. This ensures the test exercises the auth
    # proxy branch (the one affected by issue #221), not the token
    # injection branch.
    with (
        patch("bubble.cli.load_config", return_value={}),
        patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com")),
        patch("bubble.cli.get_runtime", return_value=mock_runtime),
        patch("bubble.cli.find_existing_container", return_value=None),
        patch("bubble.cli.print_warnings"),
        patch("bubble.cli.maybe_rebuild_base_image"),
        patch("bubble.cli.maybe_rebuild_tools"),
        patch("bubble.cli.maybe_rebuild_customize"),
        patch("bubble.cli.maybe_symlink_ai_projects"),
        patch("bubble.cli.RepoRegistry"),
        patch("bubble.cli.parse_target", return_value=t),
        patch("bubble.cli._resolve_ref_source", return_value=("/tmp/fake.git", "fake.git")),
        patch("bubble.cli.detect_and_build_image", return_value=(None, "base")),
        patch("bubble.cli.deduplicate_name", return_value="bubble-main"),
        patch("bubble.cli.provision_container", side_effect=mock_provision),
        patch("bubble.cli.get_github_level", return_value="allowlist-write-graphql"),
        patch("bubble.github_token.setup_gh_token", side_effect=mock_setup_gh_token),
        patch("bubble.cli.clone_and_checkout", side_effect=mock_clone),
        patch("bubble.cli.finalize_bubble", side_effect=mock_finalize),
    ):
        from click.testing import CliRunner

        from bubble.cli import main

        runner = CliRunner()
        runner.invoke(main, ["kim-em/bubble", "--no-interactive"])

    assert "provision" in call_order, f"provision not called, order: {call_order}"
    assert "setup_gh_token" in call_order, f"setup_gh_token not called, order: {call_order}"
    assert "clone" in call_order, f"clone not called, order: {call_order}"

    prov_idx = call_order.index("provision")
    auth_idx = call_order.index("setup_gh_token")
    clone_idx = call_order.index("clone")

    assert prov_idx < auth_idx < clone_idx, (
        f"Expected provision < setup_gh_token < clone, got order: {call_order}"
    )

    # Verify the proxy branch was taken (owner/repo passed, no token_inject)
    assert call_order_kwargs["owner"] == "kim-em"
    assert call_order_kwargs["repo"] == "bubble"
    assert not call_order_kwargs.get("token_inject", False)


# --- Fail-early tests: abort when auth proxy fails + network is blocked (#224) ---


def test_auth_proxy_failure_aborts_when_network_active(mock_runtime, tmp_data_dir, tmp_ssh_dir):
    """When auth proxy setup fails and network allowlisting is active, abort with clear error."""
    from bubble.target import Target

    t = Target(
        owner="kim-em",
        repo="bubble",
        kind="main",
        ref="",
        original="kim-em/bubble",
    )

    def is_enabled_side_effect(_config, setting):
        return setting != "github_token_inject"

    with (
        patch("bubble.cli.load_config", return_value={}),
        patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com")),
        patch("bubble.cli.get_runtime", return_value=mock_runtime),
        patch("bubble.cli.find_existing_container", return_value=None),
        patch("bubble.cli.print_warnings"),
        patch("bubble.cli.maybe_rebuild_base_image"),
        patch("bubble.cli.maybe_rebuild_tools"),
        patch("bubble.cli.maybe_rebuild_customize"),
        patch("bubble.cli.maybe_symlink_ai_projects"),
        patch("bubble.cli.RepoRegistry"),
        patch("bubble.cli.parse_target", return_value=t),
        patch("bubble.cli._resolve_ref_source", return_value=("/tmp/fake.git", "fake.git")),
        patch("bubble.cli.detect_and_build_image", return_value=(None, "base")),
        patch("bubble.cli.deduplicate_name", return_value="bubble-main"),
        patch("bubble.cli.provision_container"),
        patch("bubble.cli.get_github_level", return_value="allowlist-write-graphql"),
        patch("bubble.cli.is_enabled", side_effect=is_enabled_side_effect),
        patch("bubble.github_token.setup_gh_token", return_value=False),
        patch("bubble.cli.clone_and_checkout") as mock_clone,
    ):
        from click.testing import CliRunner

        from bubble.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["kim-em/bubble", "--no-interactive"])

    # Should have failed with a clear error message
    assert result.exit_code != 0
    output = result.output.lower()
    assert "auth proxy setup failed" in output
    assert "gh auth login" in result.output
    # clone_and_checkout should NOT have been called
    mock_clone.assert_not_called()


def test_auth_proxy_failure_warns_when_network_disabled(mock_runtime, tmp_data_dir, tmp_ssh_dir):
    """When auth proxy setup fails but network allowlisting is off, proceed with a warning."""
    from bubble.target import Target

    t = Target(
        owner="kim-em",
        repo="bubble",
        kind="main",
        ref="",
        original="kim-em/bubble",
    )

    def is_enabled_side_effect(_config, setting):
        return setting != "github_token_inject"

    with (
        patch("bubble.cli.load_config", return_value={}),
        patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com")),
        patch("bubble.cli.get_runtime", return_value=mock_runtime),
        patch("bubble.cli.find_existing_container", return_value=None),
        patch("bubble.cli.print_warnings"),
        patch("bubble.cli.maybe_rebuild_base_image"),
        patch("bubble.cli.maybe_rebuild_tools"),
        patch("bubble.cli.maybe_rebuild_customize"),
        patch("bubble.cli.maybe_symlink_ai_projects"),
        patch("bubble.cli.RepoRegistry"),
        patch("bubble.cli.parse_target", return_value=t),
        patch("bubble.cli._resolve_ref_source", return_value=("/tmp/fake.git", "fake.git")),
        patch("bubble.cli.detect_and_build_image", return_value=(None, "base")),
        patch("bubble.cli.deduplicate_name", return_value="bubble-main"),
        patch("bubble.cli.provision_container"),
        patch("bubble.cli.get_github_level", return_value="allowlist-write-graphql"),
        patch("bubble.cli.is_enabled", side_effect=is_enabled_side_effect),
        patch("bubble.github_token.setup_gh_token", return_value=False),
        patch("bubble.cli.clone_and_checkout", return_value="") as mock_clone,
        patch("bubble.cli.finalize_bubble"),
    ):
        from click.testing import CliRunner

        from bubble.cli import main

        runner = CliRunner()
        # --no-network disables network allowlisting, so auth failure is non-fatal
        runner.invoke(main, ["kim-em/bubble", "--no-interactive", "--no-network"])

    # Should proceed (clone should be called despite auth failure)
    mock_clone.assert_called_once()


def test_token_inject_failure_aborts_when_network_active(mock_runtime, tmp_data_dir, tmp_ssh_dir):
    """When token injection fails and network allowlisting is active, abort with clear error."""
    from bubble.target import Target

    t = Target(
        owner="kim-em",
        repo="bubble",
        kind="main",
        ref="",
        original="kim-em/bubble",
    )

    def is_enabled_side_effect(_config, setting):
        # Enable github_token_inject, disable github_auth
        return setting == "github_token_inject"

    with (
        patch("bubble.cli.load_config", return_value={}),
        patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com")),
        patch("bubble.cli.get_runtime", return_value=mock_runtime),
        patch("bubble.cli.find_existing_container", return_value=None),
        patch("bubble.cli.print_warnings"),
        patch("bubble.cli.maybe_rebuild_base_image"),
        patch("bubble.cli.maybe_rebuild_tools"),
        patch("bubble.cli.maybe_rebuild_customize"),
        patch("bubble.cli.maybe_symlink_ai_projects"),
        patch("bubble.cli.RepoRegistry"),
        patch("bubble.cli.parse_target", return_value=t),
        patch("bubble.cli._resolve_ref_source", return_value=("/tmp/fake.git", "fake.git")),
        patch("bubble.cli.detect_and_build_image", return_value=(None, "base")),
        patch("bubble.cli.deduplicate_name", return_value="bubble-main"),
        patch("bubble.cli.provision_container"),
        patch("bubble.cli.get_github_level", return_value="direct"),
        patch("bubble.cli.is_enabled", side_effect=is_enabled_side_effect),
        patch("bubble.github_token.setup_gh_token", return_value=False),
        patch("bubble.cli.clone_and_checkout") as mock_clone,
    ):
        from click.testing import CliRunner

        from bubble.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["kim-em/bubble", "--no-interactive"])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "token injection failed" in output
    assert "gh auth login" in result.output
    mock_clone.assert_not_called()
