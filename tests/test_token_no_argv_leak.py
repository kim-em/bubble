"""Security regression: tokens must not appear in argv.

These tests don't actually run incus or ssh — they intercept the calls
just before subprocess.run is invoked and assert that the token string
appears nowhere in the argv that would be visible via /proc/<pid>/cmdline
on the host or inside the container.

If a callsite ever regresses to embedding the token in a shell command,
these tests catch it.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def captured_runtime_calls(monkeypatch):
    """Make IncusRuntime.exec record its argv + input rather than running."""
    from bubble.runtime import incus as incus_mod

    calls: list[dict] = []

    def fake_exec(self, name, command, *, input=None, **kwargs):
        calls.append({"name": name, "command": list(command), "input": input})
        return ""

    monkeypatch.setattr(incus_mod.IncusRuntime, "exec", fake_exec)
    monkeypatch.setattr(incus_mod.IncusRuntime, "add_device", lambda *a, **kw: None)
    return calls


@pytest.fixture
def captured_ssh_calls(monkeypatch):
    """Intercept _ssh_run to capture argv + input without spawning ssh."""
    import subprocess

    import bubble.remote as remote_mod

    calls: list[dict] = []

    def fake_ssh_run(host, command, **kwargs):
        calls.append({"command": list(command), "input": kwargs.get("input")})
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(remote_mod, "_ssh_run", fake_ssh_run)
    return calls


SENSITIVE_TOKEN = "ghp_thisIsTheRealHostToken_doNotLeak"


def _assert_token_not_in_argv(calls: list[dict], token: str):
    """For every captured call, the token must be in input= but NEVER in argv."""
    found_in_input = False
    for call in calls:
        for arg in call["command"]:
            assert token not in str(arg), (
                f"Token leaked into argv element: {arg!r}\nFull call: {call!r}"
            )
        if call.get("input") and token in call["input"]:
            found_in_input = True
    assert found_in_input, (
        f"Token never reached the input= channel — was it dropped?\nCalls: {calls!r}"
    )


def test_setup_auth_proxy_local_does_not_leak_token(
    captured_runtime_calls, monkeypatch, tmp_data_dir
):
    """Local setup_auth_proxy never puts the per-container token in argv."""
    import bubble.github_token as gt
    import bubble.runtime.incus as incus_mod

    endpoint = {
        "tcp": {"host": "10.156.104.1", "port": 7654},
        "unix_socket": "/home/kim/.bubble/proxy-sockets/gh.sock",
        "version": 2,
    }
    monkeypatch.setattr(gt, "_ensure_auth_proxy_endpoint", lambda: endpoint)
    monkeypatch.setattr("bubble.auth_proxy.generate_auth_token", lambda *a, **kw: SENSITIVE_TOKEN)

    runtime = incus_mod.IncusRuntime()
    ok = gt.setup_auth_proxy(runtime, "my-container", "kim-em", "bubble")
    assert ok is True
    _assert_token_not_in_argv(captured_runtime_calls, SENSITIVE_TOKEN)


def test_setup_auth_proxy_remote_does_not_leak_token(captured_ssh_calls, monkeypatch, tmp_data_dir):
    """Remote setup_auth_proxy never puts the token in any SSH-shipped argv."""
    import bubble.github_token as gt

    monkeypatch.setattr(gt, "_ensure_auth_proxy_running", lambda: 7654)
    monkeypatch.setattr("bubble.tunnel.start_tunnel", lambda *a, **kw: True)
    monkeypatch.setattr("bubble.auth_proxy.generate_auth_token", lambda *a, **kw: SENSITIVE_TOKEN)

    remote_host = type("H", (), {"spec_string": lambda self: "h", "ssh_destination": "h"})()
    ok = gt.setup_auth_proxy_remote(remote_host, "my-container", "kim-em", "bubble")
    assert ok is True
    _assert_token_not_in_argv(captured_ssh_calls, SENSITIVE_TOKEN)


def test_inject_gh_token_local_does_not_leak_token(
    captured_runtime_calls, monkeypatch, tmp_data_dir
):
    import bubble.github_token as gt
    import bubble.runtime.incus as incus_mod

    monkeypatch.setattr(gt, "get_host_gh_token", lambda: SENSITIVE_TOKEN)

    runtime = incus_mod.IncusRuntime()
    ok = gt.inject_gh_token(runtime, "my-container")
    assert ok is True
    _assert_token_not_in_argv(captured_runtime_calls, SENSITIVE_TOKEN)


def test_inject_gh_token_remote_does_not_leak_token(captured_ssh_calls, monkeypatch, tmp_data_dir):
    import bubble.github_token as gt

    monkeypatch.setattr(gt, "get_host_gh_token", lambda: SENSITIVE_TOKEN)

    remote_host = type("H", (), {"spec_string": lambda self: "h", "ssh_destination": "h"})()
    ok = gt.inject_gh_token_remote(remote_host, "my-container")
    assert ok is True
    _assert_token_not_in_argv(captured_ssh_calls, SENSITIVE_TOKEN)


def test_runtime_exec_input_kwarg_pipes_to_stdin(monkeypatch):
    """IncusRuntime.exec(input=...) actually passes input= to subprocess.run."""
    import subprocess

    import bubble.runtime.incus as incus_mod

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(incus_mod.subprocess, "run", fake_run)
    rt = incus_mod.IncusRuntime()
    rt.exec("foo", ["bash"], input="hello-stdin")
    assert captured["input"] == "hello-stdin"
    # When input is passed, stdin must NOT be DEVNULL (which would block writes)
    assert captured.get("stdin") is not subprocess.DEVNULL


def test_runtime_exec_no_input_closes_stdin(monkeypatch):
    """Without input=, stdin is closed (DEVNULL) so subprocesses don't block on inherited tty."""
    import subprocess

    import bubble.runtime.incus as incus_mod

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(incus_mod.subprocess, "run", fake_run)
    rt = incus_mod.IncusRuntime()
    rt.exec("foo", ["true"])
    assert captured["stdin"] is subprocess.DEVNULL
    assert "input" not in captured
