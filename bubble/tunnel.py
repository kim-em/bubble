"""SSH reverse tunnel management for remote auth proxy access.

Establishes SSH reverse tunnels (-R) to forward the local auth proxy
port to remote hosts. This lets remote/cloud containers use the same
repo-scoped auth proxy as local containers, without injecting the
full GitHub token.

Tunnels are per-remote-host: multiple containers on the same remote
share a single tunnel. Reference counting via the bubble registry
determines when a tunnel can be torn down.
"""

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

from .config import DATA_DIR

TUNNEL_DIR = DATA_DIR / "tunnels"

# Default remote-side port for the auth proxy tunnel
TUNNEL_REMOTE_PORT = 7654


def _sanitize_host_spec(spec: str) -> str:
    """Sanitize a host spec string for use as a filename."""
    return spec.replace("/", "_").replace(":", "_").replace("@", "_")


def _pid_file(host_spec: str) -> Path:
    """Return the PID file path for a remote host's tunnel."""
    return TUNNEL_DIR / f"{_sanitize_host_spec(host_spec)}.pid"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def is_tunnel_alive(host_spec: str) -> bool:
    """Check if a tunnel to the given remote host is running."""
    pf = _pid_file(host_spec)
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
        return _is_process_alive(pid)
    except (ValueError, OSError):
        return False


def _lock_file(host_spec: str) -> Path:
    """Return the lock file path for a remote host's tunnel."""
    return TUNNEL_DIR / f"{_sanitize_host_spec(host_spec)}.lock"


def start_tunnel(remote_host, local_port: int, remote_port: int = TUNNEL_REMOTE_PORT) -> bool:
    """Start an SSH reverse tunnel to a remote host.

    Creates an SSH connection with -R to forward remote_port on the
    remote host back to local_port on the local machine. The tunnel
    persists as a background process.

    If a tunnel to this host is already running, returns True without
    starting another.

    Uses file locking to prevent concurrent callers from spawning
    duplicate SSH processes for the same host.

    Returns True if the tunnel is running (started or already existed).
    """
    host_spec = remote_host.spec_string()

    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)

    # Lock to prevent two concurrent opens from spawning duplicate tunnels
    lf = _lock_file(host_spec)
    fd = lf.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)

        # Re-check under lock — another process may have started the tunnel
        if is_tunnel_alive(host_spec):
            return True

        # Build SSH tunnel command
        cmd = ["ssh"]
        if remote_host.ssh_options:
            cmd += remote_host.ssh_options
        if remote_host.port != 22:
            cmd += ["-p", str(remote_host.port)]
        cmd += [
            "-N",  # No remote command
            "-o",
            "ExitOnForwardFailure=yes",  # Fail if port forward fails
            "-o",
            "ServerAliveInterval=30",  # Keepalive every 30s
            "-o",
            "ServerAliveCountMax=3",  # Give up after 3 missed keepalives
            "-R",
            f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
            remote_host.ssh_destination,
        ]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait briefly for the tunnel to establish or fail
        try:
            proc.wait(timeout=10)
            # Process exited — tunnel failed to start
            return False
        except subprocess.TimeoutExpired:
            pass

        # Process is still running — tunnel is up
        _pid_file(host_spec).write_text(str(proc.pid))
        return True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def stop_tunnel(host_spec: str) -> bool:
    """Stop the SSH tunnel to a remote host.

    Returns True if the tunnel was stopped (or wasn't running).
    """
    pf = _pid_file(host_spec)
    if not pf.exists():
        return True

    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        pf.unlink(missing_ok=True)
        return True

    if _is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for clean shutdown
            for _ in range(10):
                if not _is_process_alive(pid):
                    break
                time.sleep(0.1)
            else:
                # Force kill if still alive
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    pf.unlink(missing_ok=True)
    return True


def stop_tunnel_if_unused(host_spec: str) -> bool:
    """Stop the tunnel to a remote host only if no other bubbles use it.

    Checks the bubble registry for other active bubbles on the same
    remote host. If none remain, stops the tunnel.

    Returns True if the tunnel was stopped or no action was needed.
    """
    from .lifecycle import load_registry

    registry = load_registry()
    for _name, info in registry.get("bubbles", {}).items():
        remote = info.get("remote_host", "")
        if remote == host_spec:
            # Another bubble still uses this remote host
            return True

    return stop_tunnel(host_spec)
