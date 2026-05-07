"""Tests for `bubble open --github-security <level>` per-launch override."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bubble.cli import main
from bubble.security import get_github_level
from bubble.target import Target


def _apply_open_patches(stack, *, captured_config=None):
    """Patch the open-command dependencies. If `captured_config` is a list,
    `get_github_level` is replaced with a wrapper that records every config
    it sees, allowing tests to assert the override propagated.

    Stops execution at the network-allowlist step so we capture the effective
    level at the point that matters but skip the heavy provisioning code.
    """
    stack.enter_context(
        patch(
            "bubble.cli.load_config",
            return_value={"security": {"github": "auto"}},
        )
    )
    stack.enter_context(patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com")))
    rt_mock = stack.enter_context(patch("bubble.cli.get_runtime"))
    rt_mock.return_value.list_containers.return_value = []
    stack.enter_context(patch("bubble.cli.find_existing_container", return_value=None))
    stack.enter_context(patch("bubble.cli.print_warnings"))
    stack.enter_context(patch("bubble.cli.maybe_rebuild_base_image"))
    stack.enter_context(patch("bubble.cli.maybe_rebuild_tools"))
    stack.enter_context(patch("bubble.cli.maybe_rebuild_customize"))
    stack.enter_context(patch("bubble.cli.maybe_symlink_ai_projects"))
    stack.enter_context(patch("bubble.cli.RepoRegistry"))

    target = Target(
        owner="kim-em",
        repo="bubble",
        kind="repo",
        ref="",
        original="kim-em/bubble",
    )
    stack.enter_context(patch("bubble.cli.parse_target", return_value=target))
    stack.enter_context(
        patch("bubble.cli._resolve_ref_source", return_value=("/tmp/fake.git", "fake.git"))
    )
    stack.enter_context(patch("bubble.cli.detect_and_build_image", return_value=(None, "img")))
    stack.enter_context(patch("bubble.cli.generate_name", return_value="test-bubble"))

    if captured_config is not None:
        real_get_level = get_github_level

        def _capture(config):
            captured_config.append(config)
            return real_get_level(config)

        # Patch at the source so lazy `from .security import get_github_level`
        # callers (apply_network, _resolve_rest_api, _resolve_graphql_config,
        # _open_remote, _open_single's auth-setup branch) all see the mock.
        stack.enter_context(patch("bubble.security.get_github_level", _capture))
        stack.enter_context(patch("bubble.cli.get_github_level", _capture))

    # Short-circuit at provision_container so we exercise the lockdown check
    # and config mutation but skip Incus calls.
    stack.enter_context(patch("bubble.cli.provision_container", side_effect=SystemExit(0)))


class TestLockdownStillWins:
    def test_override_rejected_when_security_github_locked_off(self):
        """`--github-security` is rejected with explanatory error when host
        has `security.github=off` (lockdown). Must not silently downgrade."""
        runner = CliRunner()
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "bubble.cli.load_config",
                    return_value={"security": {"github": "off"}},
                )
            )
            result = runner.invoke(
                main,
                ["open", "--github-security", "write-graphql", "kim-em/bubble"],
            )
        assert result.exit_code != 0
        assert "rejected because security.github=off" in (result.output + (result.stderr or ""))


class TestClickChoiceValidation:
    def test_invalid_level_rejected_at_parse_time(self):
        """Bogus values are rejected by click.Choice before any handler runs."""
        runner = CliRunner()
        result = runner.invoke(main, ["open", "--github-security", "bogus", "kim-em/bubble"])
        assert result.exit_code != 0
        assert "bogus" in (result.output + (result.stderr or ""))


class TestLocalOverridePropagation:
    """The override mutates a config copy in `_open_single`; downstream
    helpers consult that same config. We capture the config that reaches
    `provision_container` and assert `get_github_level` resolves it
    correctly."""

    def _run_and_capture_config(self, *cli_args, host_config=None):
        captured_configs = []

        def _capture_provision(*args, **kw):
            # signature: provision_container(runtime, name, image_name,
            #     ref_path, mount_name, config, ...)
            if len(args) >= 6:
                captured_configs.append(args[5])
            elif "config" in kw:
                captured_configs.append(kw["config"])
            raise SystemExit(0)

        runner = CliRunner()
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "bubble.cli.load_config",
                    return_value=host_config or {"security": {"github": "auto"}},
                )
            )
            stack.enter_context(
                patch("bubble.cli.get_host_git_identity", return_value=("Test", "t@t.com"))
            )
            rt_mock = stack.enter_context(patch("bubble.cli.get_runtime"))
            rt_mock.return_value.list_containers.return_value = []
            stack.enter_context(patch("bubble.cli.find_existing_container", return_value=None))
            stack.enter_context(patch("bubble.cli.print_warnings"))
            stack.enter_context(patch("bubble.cli.maybe_rebuild_base_image"))
            stack.enter_context(patch("bubble.cli.maybe_rebuild_tools"))
            stack.enter_context(patch("bubble.cli.maybe_rebuild_customize"))
            stack.enter_context(patch("bubble.cli.maybe_symlink_ai_projects"))
            stack.enter_context(patch("bubble.cli.RepoRegistry"))
            target = Target(
                owner="kim-em", repo="bubble", kind="repo", ref="", original="kim-em/bubble"
            )
            stack.enter_context(patch("bubble.cli.parse_target", return_value=target))
            stack.enter_context(
                patch(
                    "bubble.cli._resolve_ref_source",
                    return_value=("/tmp/fake.git", "fake.git"),
                )
            )
            stack.enter_context(
                patch("bubble.cli.detect_and_build_image", return_value=(None, "img"))
            )
            stack.enter_context(patch("bubble.cli.generate_name", return_value="test-bubble"))
            stack.enter_context(patch("bubble.cli.provision_container", _capture_provision))
            runner.invoke(main, ["open", *cli_args])
        return captured_configs

    def test_override_drives_direct_level(self):
        """`--github-security direct` from a non-direct host should be
        observed by downstream helpers that read `get_github_level(config)`."""
        captured = self._run_and_capture_config("--github-security", "direct", "kim-em/bubble")
        assert captured, "provision_container was never invoked"
        for cfg in captured:
            assert get_github_level(cfg) == "direct"

    def test_override_off_propagates(self):
        """`--github-security off` propagates to downstream config so
        apply_network's filter_github_domains() branch fires."""
        captured = self._run_and_capture_config("--github-security", "off", "kim-em/bubble")
        assert captured
        for cfg in captured:
            assert get_github_level(cfg) == "off"

    def test_no_override_uses_host_config(self):
        """Without `--github-security`, level resolves from host config."""
        captured = self._run_and_capture_config(
            "kim-em/bubble", host_config={"security": {"github": "rest"}}
        )
        assert captured
        for cfg in captured:
            assert get_github_level(cfg) == "rest"

    def test_override_does_not_persist_to_disk(self):
        """The override mutates an in-memory config copy only; the host's
        loaded config dict must remain untouched (no write back to disk)."""
        host_config = {"security": {"github": "auto"}}
        # Use the dict identity to verify we mutated a COPY.
        captured = self._run_and_capture_config(
            "--github-security",
            "write-graphql",
            "kim-em/bubble",
            host_config=host_config,
        )
        assert captured
        # The provisioning call must have seen a different dict (the copy).
        assert captured[0] is not host_config
        # The original host config dict must still be auto.
        assert host_config["security"]["github"] == "auto"


class TestRemoteForwarding:
    def test_remote_open_argv_includes_github_security(self):
        """`remote_open()` appends `--github-security <level>` to the
        remote-side `bubble open` argv."""
        from bubble import remote

        captured_argv = []

        class _FakeProc:
            def __init__(self, *a, **kw):
                # Capture the ssh_cmd argv. The shell-quoted bubble
                # invocation is the last element of ssh_cmd.
                captured_argv.append(a[0] if a else kw.get("args"))
                self.stdout = iter(['{"name": "x", "project_dir": "/home/user"}\n'])
                self.stderr = MagicMock()
                self.stderr.read.return_value = ""
                self.stdin = None
                self.returncode = 0

            def wait(self, *a, **kw):
                return 0

        with ExitStack() as stack:
            stack.enter_context(patch("bubble.remote.ensure_remote_bubble"))
            stack.enter_context(patch("bubble.remote._find_remote_python", return_value="python3"))
            stack.enter_context(patch("bubble.remote.subprocess.Popen", _FakeProc))
            host = MagicMock()
            host.ssh_destination = "user@example.com"
            host.ssh_cmd = lambda parts: ["ssh", host.ssh_destination] + parts
            try:
                remote.remote_open(
                    host,
                    "kim-em/bubble",
                    github_security="write-graphql",
                )
            except Exception:
                # JSON parsing of fake stdout may fail; we only care about argv.
                pass

        assert captured_argv, "Popen was never invoked"
        joined = " ".join(captured_argv[0])
        assert "--github-security" in joined
        assert "write-graphql" in joined

    def test_remote_open_no_arg_when_github_security_unset(self):
        """When the override is None, no `--github-security` flag is added
        (so the remote falls back to its own host config)."""
        from bubble import remote

        captured_argv = []

        class _FakeProc:
            def __init__(self, *a, **kw):
                captured_argv.append(a[0] if a else kw.get("args"))
                self.stdout = iter(['{"name": "x", "project_dir": "/home/user"}\n'])
                self.stderr = MagicMock()
                self.stderr.read.return_value = ""
                self.stdin = None
                self.returncode = 0

            def wait(self, *a, **kw):
                return 0

        with ExitStack() as stack:
            stack.enter_context(patch("bubble.remote.ensure_remote_bubble"))
            stack.enter_context(patch("bubble.remote._find_remote_python", return_value="python3"))
            stack.enter_context(patch("bubble.remote.subprocess.Popen", _FakeProc))
            host = MagicMock()
            host.ssh_destination = "user@example.com"
            host.ssh_cmd = lambda parts: ["ssh", host.ssh_destination] + parts
            try:
                remote.remote_open(host, "kim-em/bubble")
            except Exception:
                pass

        joined = " ".join(captured_argv[0])
        assert "--github-security" not in joined


class TestOpenRemoteForwarding:
    def test_open_remote_passes_override_to_remote_open(self):
        """`_open_remote()` forwards its `github_security` parameter to
        `remote_open()` (closes the gap Codex flagged)."""
        from bubble import cli as cli_mod

        captured_kwargs = {}

        def _fake_remote_open(host, target, **kw):
            captured_kwargs.update(kw)
            return {"name": "x", "project_dir": "/home/user", "org_repo": "kim-em/bubble"}

        with ExitStack() as stack:
            # remote_open is lazy-imported inside _open_remote, so patch the
            # source module rather than `bubble.cli`.
            stack.enter_context(patch("bubble.remote.remote_open", _fake_remote_open))
            stack.enter_context(patch("bubble.cli._resolve_ai_prompt_locally", return_value=""))
            stack.enter_context(patch("bubble.cli.inject_local_ssh_keys"))
            # Stop execution after remote_open returns, before auth-setup
            stack.enter_context(patch("bubble.cli.get_github_level", side_effect=SystemExit(0)))

            host = MagicMock()
            host.ssh_destination = "user@example.com"
            try:
                cli_mod._open_remote(
                    host,
                    "kim-em/bubble",
                    editor="shell",
                    no_interactive=True,
                    network=True,
                    custom_name=None,
                    config={"security": {"github": "auto"}},
                    github_security="write-graphql",
                )
            except SystemExit:
                pass

        assert captured_kwargs.get("github_security") == "write-graphql"
