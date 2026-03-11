"""Tests for SSH reverse tunnel management."""

import os
import signal
from unittest.mock import MagicMock, patch

from bubble.tunnel import (
    TUNNEL_DIR,
    _pid_file,
    _sanitize_host_spec,
    is_tunnel_alive,
    start_tunnel,
    stop_tunnel,
    stop_tunnel_if_unused,
)


def test_sanitize_host_spec():
    assert _sanitize_host_spec("root@1.2.3.4") == "root_1.2.3.4"
    assert _sanitize_host_spec("user@host:2222") == "user_host_2222"
    assert _sanitize_host_spec("myhost") == "myhost"


def test_pid_file_path(tmp_data_dir):
    pf = _pid_file("root_1.2.3.4")
    assert pf == TUNNEL_DIR / "root_1.2.3.4.pid"


def test_is_tunnel_alive_no_pid_file(tmp_data_dir):
    assert is_tunnel_alive("root_1.2.3.4") is False


def test_is_tunnel_alive_with_dead_pid(tmp_data_dir):
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = _pid_file("root_1.2.3.4")
    pf.write_text("999999999")  # Very unlikely to be a real PID
    assert is_tunnel_alive("root_1.2.3.4") is False


def test_is_tunnel_alive_with_own_pid(tmp_data_dir):
    """Our own PID should be 'alive' (for testing purposes)."""
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = _pid_file("root_1.2.3.4")
    pf.write_text(str(os.getpid()))
    assert is_tunnel_alive("root_1.2.3.4") is True


def test_start_tunnel_already_running(tmp_data_dir):
    """start_tunnel returns True if tunnel already alive."""
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    host = MagicMock()
    host.spec_string.return_value = "myhost"

    pf = _pid_file("myhost")
    pf.write_text(str(os.getpid()))  # Use our own PID as "alive"

    result = start_tunnel(host, local_port=7654)
    assert result is True


def test_start_tunnel_ssh_fails(tmp_data_dir):
    """start_tunnel returns False if SSH process exits immediately."""
    host = MagicMock()
    host.spec_string.return_value = "badhost"
    host.ssh_options = None
    host.port = 22
    host.ssh_destination = "badhost"

    mock_proc = MagicMock()
    mock_proc.wait.return_value = 1  # Exits immediately (no timeout)
    mock_proc.pid = 12345

    with patch("bubble.tunnel.subprocess.Popen", return_value=mock_proc):
        result = start_tunnel(host, local_port=7654)
        assert result is False


def test_start_tunnel_success(tmp_data_dir):
    """start_tunnel returns True and writes PID when SSH stays running."""
    import subprocess

    host = MagicMock()
    host.spec_string.return_value = "goodhost"
    host.ssh_options = None
    host.port = 22
    host.ssh_destination = "goodhost"

    mock_proc = MagicMock()
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=10)
    mock_proc.pid = 54321

    with patch("bubble.tunnel.subprocess.Popen", return_value=mock_proc):
        result = start_tunnel(host, local_port=7654)
        assert result is True

    pf = _pid_file("goodhost")
    assert pf.exists()
    assert pf.read_text() == "54321"


def test_stop_tunnel_no_pid_file(tmp_data_dir):
    assert stop_tunnel("nonexistent") is True


def test_stop_tunnel_dead_process(tmp_data_dir):
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = _pid_file("deadhost")
    pf.write_text("999999999")

    result = stop_tunnel("deadhost")
    assert result is True
    assert not pf.exists()


def test_stop_tunnel_kills_process(tmp_data_dir):
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = _pid_file("livehost")
    pf.write_text("12345")

    kill_calls = []

    def mock_kill(pid, sig):
        kill_calls.append((pid, sig))
        if sig == signal.SIGTERM:
            return  # First call succeeds
        if sig == 0:
            if len([c for c in kill_calls if c[1] == 0]) > 1:
                raise ProcessLookupError  # Process is gone after SIGTERM
            return  # First check says alive

    with patch("bubble.tunnel.os.kill", side_effect=mock_kill):
        result = stop_tunnel("livehost")
        assert result is True
        assert not pf.exists()
        # Should have sent SIGTERM
        assert any(sig == signal.SIGTERM for _, sig in kill_calls)


def test_stop_tunnel_if_unused_has_other_bubbles(tmp_data_dir):
    """Don't stop tunnel if other bubbles use the same remote host."""
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = _pid_file("myhost")
    pf.write_text(str(os.getpid()))

    registry = {"other-bubble": {"remote_host": "myhost"}}
    with patch("bubble.lifecycle.load_registry", return_value=registry):
        result = stop_tunnel_if_unused("myhost")
        assert result is True
        # PID file should still exist (tunnel not stopped)
        assert pf.exists()


def test_stop_tunnel_if_unused_no_other_bubbles(tmp_data_dir):
    """Stop tunnel if no other bubbles use the same remote host."""
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    pf = _pid_file("myhost")
    pf.write_text("999999999")  # Dead PID

    registry = {"other-bubble": {"remote_host": "otherhost"}}
    with patch("bubble.lifecycle.load_registry", return_value=registry):
        result = stop_tunnel_if_unused("myhost")
        assert result is True
        assert not pf.exists()
