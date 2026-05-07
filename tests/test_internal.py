"""Tests for the hidden 'bubble internal' RPC subcommands.

These commands run on a remote bubble host (over SSH) so the local side
can act on a container without shipping raw ``incus`` argv.  We don't
actually exec real incus here — we install a mock runtime and verify
exactly what arguments it would have received.
"""

from click.testing import CliRunner

from bubble.cli import main


class _RecordingRuntime:
    """Stand-in for IncusRuntime that records calls without executing."""

    def __init__(self):
        self.exec_calls: list[tuple] = []
        self.add_device_calls: list[tuple] = []

    def exec(self, name, command, *, input=None, **kwargs):
        self.exec_calls.append((name, list(command), input))
        return "ok"

    def add_device(self, name, device_name, device_type, **props):
        self.add_device_calls.append((name, device_name, device_type, props))


def _patch_runtime(monkeypatch, rt):
    """Make ``get_runtime`` in commands.internal return our recorder."""
    import bubble.commands.internal as internal_mod

    monkeypatch.setattr(internal_mod, "get_runtime", lambda *args, **kwargs: rt)


def test_help_lists_subcommands():
    """The 'internal' group itself should expose its verbs in --help."""
    runner = CliRunner()
    result = runner.invoke(main, ["internal", "--help"])
    assert result.exit_code == 0
    assert "incus-exec" in result.output
    assert "incus-add-device" in result.output


def test_internal_hidden_from_top_level_help():
    """The 'internal' group must not appear in top-level --help (it's IPC)."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "internal" not in result.output.lower().split()


def test_incus_exec_forwards_argv(monkeypatch, tmp_data_dir):
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["internal", "incus-exec", "my-bubble", "bash", "-c", "echo hello"],
    )
    assert result.exit_code == 0
    assert rt.exec_calls == [("my-bubble", ["bash", "-c", "echo hello"], None)]
    # Output is forwarded
    assert "ok" in result.output


def test_incus_exec_requires_argv(monkeypatch, tmp_data_dir):
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(main, ["internal", "incus-exec", "my-bubble"])
    assert result.exit_code != 0  # Click rejects missing required argv
    assert rt.exec_calls == []


def test_incus_exec_passes_dashes_through(monkeypatch, tmp_data_dir):
    """Dashes in the command payload must reach the runtime, not be parsed by Click."""
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["internal", "incus-exec", "my-bubble", "ls", "-la", "--color=auto"],
    )
    assert result.exit_code == 0
    assert rt.exec_calls == [("my-bubble", ["ls", "-la", "--color=auto"], None)]


def test_incus_exec_with_stdin_pipes_stdin_to_runtime(monkeypatch, tmp_data_dir):
    """--with-stdin reads our stdin and passes it to runtime.exec(input=...)."""
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["internal", "incus-exec", "--with-stdin", "my-bubble", "bash", "-c", "cat"],
        input="secret-token\n",
    )
    assert result.exit_code == 0
    # Token reached the runtime via the input= kwarg, not argv
    assert rt.exec_calls == [
        ("my-bubble", ["bash", "-c", "cat"], "secret-token\n"),
    ]


def test_incus_exec_without_with_stdin_does_not_read_stdin(monkeypatch, tmp_data_dir):
    """Without --with-stdin, input= stays None even if stdin has content."""
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["internal", "incus-exec", "my-bubble", "true"],
        input="should-be-ignored\n",
    )
    assert result.exit_code == 0
    assert rt.exec_calls == [("my-bubble", ["true"], None)]


def test_incus_exec_runtime_error_exits_nonzero(monkeypatch, tmp_data_dir):
    class _Failing:
        def exec(self, name, command, **kwargs):
            raise RuntimeError("kaboom")

    _patch_runtime(monkeypatch, _Failing())
    runner = CliRunner()
    result = runner.invoke(main, ["internal", "incus-exec", "my-bubble", "true"])
    assert result.exit_code == 1
    assert "kaboom" in result.output


def test_incus_add_device_parses_props(monkeypatch, tmp_data_dir):
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "internal",
            "incus-add-device",
            "my-bubble",
            "bubble-auth-proxy",
            "proxy",
            "connect=tcp:127.0.0.1:7654",
            "listen=tcp:127.0.0.1:8888",
            "bind=container",
        ],
    )
    assert result.exit_code == 0
    assert rt.add_device_calls == [
        (
            "my-bubble",
            "bubble-auth-proxy",
            "proxy",
            {
                "connect": "tcp:127.0.0.1:7654",
                "listen": "tcp:127.0.0.1:8888",
                "bind": "container",
            },
        )
    ]


def test_incus_add_device_rejects_bare_value(monkeypatch, tmp_data_dir):
    """A prop without '=' is not a structured argv element — refuse it."""
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["internal", "incus-add-device", "my-bubble", "dev", "proxy", "no-equals-here"],
    )
    assert result.exit_code == 2
    assert "Invalid prop" in result.output
    assert rt.add_device_calls == []


def test_incus_add_device_rejects_empty_key(monkeypatch, tmp_data_dir):
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main, ["internal", "incus-add-device", "my-bubble", "dev", "proxy", "=value"]
    )
    assert result.exit_code == 2
    assert "empty key" in result.output


def test_incus_add_device_value_can_contain_equals(monkeypatch, tmp_data_dir):
    """Only the FIRST '=' splits key from value; the rest stays in the value."""
    rt = _RecordingRuntime()
    _patch_runtime(monkeypatch, rt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "internal",
            "incus-add-device",
            "my-bubble",
            "dev",
            "proxy",
            "raw.lxc=lxc.cap.drop = sys_admin",
        ],
    )
    assert result.exit_code == 0
    assert rt.add_device_calls == [
        ("my-bubble", "dev", "proxy", {"raw.lxc": "lxc.cap.drop = sys_admin"}),
    ]
