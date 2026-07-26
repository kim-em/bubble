"""Tests for systemd PATH environment injection."""

import plistlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bubble.automation import (
    _AUTH_PROXY_JOB,
    _bubble_path,
    _install_artifact_cache_systemd,
    _install_auth_proxy_systemd,
    _systemd_path_env,
    _write_launchd_plist,
    install_auth_proxy_daemon,
)


def test_systemd_path_env_uses_current_path():
    """PATH from the current environment is captured."""
    with patch.dict("os.environ", {"PATH": "/usr/local/bin:/usr/bin:/bin"}):
        result = _systemd_path_env()
        assert result == "Environment=PATH=/usr/local/bin:/usr/bin:/bin"


def test_systemd_path_env_escapes_percent():
    """Percent signs are escaped for systemd specifier syntax."""
    with patch.dict("os.environ", {"PATH": "/home/user/%n/bin:/usr/bin"}):
        result = _systemd_path_env()
        assert result == "Environment=PATH=/home/user/%%n/bin:/usr/bin"


def test_systemd_path_env_fallback():
    """Falls back to sensible default when PATH is unset."""
    with patch.dict("os.environ", {}, clear=True):
        result = _systemd_path_env()
        assert result == "Environment=PATH=/usr/local/bin:/usr/bin:/bin"


def test_bubble_path_prefers_invoked_cli(tmp_path, monkeypatch):
    import sys

    invoked = tmp_path / "bubble"
    invoked.touch()
    monkeypatch.setattr(sys, "argv", [str(invoked)])
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/bubble")

    assert _bubble_path() == str(invoked.resolve())


def test_launchd_replaces_loaded_label_and_checks_bootstrap(tmp_path, monkeypatch):
    import bubble.automation as automation

    host_data = tmp_path / ".bubble"
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(automation, "HOST_DATA_DIR", host_data)
    monkeypatch.setattr(automation, "_bubble_path", lambda: "/stable/bin/bubble")
    monkeypatch.setattr(automation.subprocess, "run", run)
    monkeypatch.setattr(automation.os, "getuid", lambda: 501)

    dst = _write_launchd_plist("com.bubble.auth-proxy", _AUTH_PROXY_JOB, host_global=True)

    assert calls[0] == ["launchctl", "bootout", "gui/501/com.bubble.auth-proxy"]
    assert calls[1][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert len(calls) == 2
    with open(dst, "rb") as f:
        assert plistlib.load(f)["ProgramArguments"][0] == "/stable/bin/bubble"


def test_launchd_surfaces_bootstrap_failure(tmp_path, monkeypatch):
    import bubble.automation as automation

    monkeypatch.setattr(automation, "HOST_DATA_DIR", tmp_path / ".bubble")
    monkeypatch.setattr(automation, "_bubble_path", lambda: "/stable/bin/bubble")
    monkeypatch.setattr(automation.time, "sleep", lambda _seconds: None)

    def run(argv, **_kwargs):
        rc = 1 if argv[1] == "bootstrap" else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="bootstrap failed")

    monkeypatch.setattr(automation.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        _write_launchd_plist("com.bubble.auth-proxy", _AUTH_PROXY_JOB, host_global=True)


def test_launchd_retries_transient_bootstrap_failure(tmp_path, monkeypatch):
    import bubble.automation as automation

    calls = []
    bootstraps = 0
    monkeypatch.setattr(automation, "HOST_DATA_DIR", tmp_path / ".bubble")
    monkeypatch.setattr(automation, "_bubble_path", lambda: "/stable/bin/bubble")
    monkeypatch.setattr(automation.time, "sleep", lambda _seconds: None)

    def run(argv, **_kwargs):
        nonlocal bootstraps
        calls.append(argv)
        if argv[1] == "bootstrap":
            bootstraps += 1
            rc = 1 if bootstraps == 1 else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="transient EIO")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(automation.subprocess, "run", run)
    _write_launchd_plist("com.bubble.auth-proxy", _AUTH_PROXY_JOB, host_global=True)
    assert bootstraps == 2
    assert calls[-1][1] == "bootstrap"


def test_systemd_restarts_active_auth_proxy(tmp_path, monkeypatch):
    import bubble.automation as automation

    calls = []
    monkeypatch.setattr(automation, "HOST_SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(automation, "_bubble_path", lambda: "/stable/bin/bubble")
    monkeypatch.setattr(
        automation.subprocess,
        "run",
        lambda argv, **_kwargs: (
            calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    _install_auth_proxy_systemd()

    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "bubble-auth-proxy.service"],
        ["systemctl", "--user", "restart", "bubble-auth-proxy.service"],
    ]


def test_systemd_restarts_active_artifact_cache(tmp_path, monkeypatch):
    import bubble.automation as automation

    calls = []
    monkeypatch.setattr(automation, "HOST_SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(automation, "_bubble_path", lambda: "/stable/bin/bubble")
    monkeypatch.setattr(
        automation.subprocess,
        "run",
        lambda argv, **_kwargs: (
            calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    _install_artifact_cache_systemd()

    assert (
        "ExecStart=/stable/bin/bubble cache daemon"
        in (tmp_path / "bubble-artifact-cache.service").read_text()
    )
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "bubble-artifact-cache.service"],
        ["systemctl", "--user", "restart", "bubble-artifact-cache.service"],
    ]


def test_systemd_surfaces_restart_failure(tmp_path, monkeypatch):
    import bubble.automation as automation

    monkeypatch.setattr(automation, "HOST_SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(automation, "_bubble_path", lambda: "/stable/bin/bubble")

    def run(argv, **_kwargs):
        rc = 1 if "restart" in argv else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="restart failed")

    monkeypatch.setattr(automation.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="restart failed"):
        _install_auth_proxy_systemd()


def test_auth_proxy_install_rechecks_health_under_host_lock(tmp_path, monkeypatch):
    import bubble.automation as automation

    monkeypatch.setattr(automation, "HOST_DATA_DIR", tmp_path / ".bubble")
    monkeypatch.setattr(
        automation.platform,
        "system",
        lambda: (_ for _ in ()).throw(AssertionError("must skip installation")),
    )
    assert install_auth_proxy_daemon(skip_if=lambda: True) == "already running"
    assert (tmp_path / ".bubble/auth-proxy.install.lock").exists()
