"""GitHub authentication for containers via auth proxy.

Instead of injecting the host's full GitHub token into containers,
we run an HTTP reverse proxy on the host that:
1. Receives plain HTTP git requests from the container
2. Validates the request targets only the allowed repository
3. Adds the real Authorization header
4. Forwards to GitHub over HTTPS

The host GitHub token never enters the container. Each container
gets a per-container bearer token scoped to one repository.

For local containers, the proxy is exposed via Incus proxy devices.
For remote/cloud containers, an SSH reverse tunnel forwards the
local proxy port to the remote host, then an Incus proxy device
on the remote exposes it into the container.
"""

import platform
import shlex
import subprocess

from .output import detail
from .runtime.base import ContainerRuntime

# Port inside the container where the auth proxy is exposed
_CONTAINER_PROXY_PORT = 7654


def get_host_gh_token() -> str | None:
    """Get the GitHub auth token from the host's gh CLI.

    Returns the token string, or None if gh is not authenticated.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def has_gh_auth() -> bool:
    """Check if the host has gh CLI authentication configured.

    Uses `gh auth status` instead of retrieving the actual token,
    to avoid unnecessary secret handling for a UX check.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_auth_proxy_running() -> int | None:
    """Ensure the auth proxy daemon is running. Returns the port, or None.

    Installs the daemon if not already installed, then checks the port file.
    """
    from .auth_proxy import AUTH_PROXY_PORT_FILE
    from .automation import install_auth_proxy_daemon, is_auth_proxy_installed

    if not is_auth_proxy_installed():
        install_auth_proxy_daemon()

    # Give daemon a moment to start and write port file
    import time

    for _ in range(10):
        if AUTH_PROXY_PORT_FILE.exists():
            try:
                return int(AUTH_PROXY_PORT_FILE.read_text().strip())
            except (ValueError, OSError):
                pass
        time.sleep(0.5)

    return None


def _colima_host_ip() -> str:
    """Get the host IP as seen from inside the Colima VM."""
    try:
        result = subprocess.run(
            ["colima", "ssh", "--", "getent", "hosts", "host.lima.internal"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "192.168.5.2"  # Default Colima host IP


def setup_auth_proxy(
    runtime: ContainerRuntime,
    container: str,
    owner: str,
    repo: str,
    machine_readable: bool = False,
) -> bool:
    """Set up auth proxy access for a container.

    1. Ensure auth proxy daemon is running
    2. Generate per-container token scoped to owner/repo
    3. Add Incus proxy device exposing the proxy into the container
    4. Configure git inside the container to use the proxy

    Returns True if setup succeeded.
    """
    from .auth_proxy import generate_auth_token

    port = _ensure_auth_proxy_running()
    if not port:
        if not machine_readable:
            detail("Warning: auth proxy failed to start. No GitHub auth configured.")
            detail("Run 'bubble gh proxy start' to diagnose.")
        return False

    # Generate per-container token
    token = generate_auth_token(container, owner, repo)

    # Add Incus proxy device: expose host TCP port into container
    # On macOS (Colima), need to use the host IP from the VM's perspective
    if platform.system() == "Darwin":
        host_ip = _colima_host_ip()
    else:
        host_ip = "127.0.0.1"

    connect_addr = f"tcp:{host_ip}:{port}"
    listen_addr = f"tcp:127.0.0.1:{_CONTAINER_PROXY_PORT}"

    try:
        runtime.add_device(
            container,
            "bubble-auth-proxy",
            "proxy",
            connect=connect_addr,
            listen=listen_addr,
            bind="container",
        )
    except Exception as e:
        if not machine_readable:
            detail(f"Warning: failed to add proxy device: {e}")
            detail("No GitHub auth configured (fail-closed).")
        return False

    # Configure git inside the container to use the proxy
    q_token = shlex.quote(token)
    git_config_cmd = (
        f"git config --global url.'http://127.0.0.1:{_CONTAINER_PROXY_PORT}/git/'.insteadOf"
        f" 'https://github.com/'"
        f" && git config --global http.'http://127.0.0.1:{_CONTAINER_PROXY_PORT}/'.extraHeader"
        f" 'X-Bubble-Token: {q_token}'"
    )

    try:
        runtime.exec(
            container,
            ["bash", "-c", f"su - user -c {shlex.quote(git_config_cmd)}"],
        )
    except RuntimeError as e:
        if not machine_readable:
            detail(f"Warning: failed to configure git proxy: {e}")
            detail("No GitHub auth configured (fail-closed).")
        return False

    if not machine_readable:
        detail(f"GitHub auth proxy configured (scoped to {owner}/{repo}).")
    return True


def setup_auth_proxy_remote(
    remote_host,
    container: str,
    owner: str,
    repo: str,
    machine_readable: bool = False,
) -> bool:
    """Set up auth proxy access for a container on a remote host.

    Tunnels the local auth proxy to the remote host via SSH reverse
    port forwarding, adds an Incus proxy device on the remote to
    expose the tunneled port into the container, and configures git.

    The host GitHub token never leaves the local machine.

    Returns True if setup succeeded.
    """
    from .auth_proxy import generate_auth_token, remove_auth_tokens
    from .remote import _ssh_run
    from .tunnel import start_tunnel

    port = _ensure_auth_proxy_running()
    if not port:
        if not machine_readable:
            detail("Warning: auth proxy failed to start. No GitHub auth configured.")
            detail("Run 'bubble gh proxy start' to diagnose.")
        return False

    # Start SSH reverse tunnel (per-remote-host, shared across containers)
    if not start_tunnel(remote_host, local_port=port):
        if not machine_readable:
            detail("Warning: SSH tunnel to remote failed. No GitHub auth configured.")
        return False

    # Generate per-container token
    token = generate_auth_token(container, owner, repo)

    # Add Incus proxy device on the remote: tunneled port → container
    from .tunnel import TUNNEL_REMOTE_PORT

    connect_addr = f"tcp:127.0.0.1:{TUNNEL_REMOTE_PORT}"
    listen_addr = f"tcp:127.0.0.1:{_CONTAINER_PROXY_PORT}"

    try:
        _ssh_run(
            remote_host,
            [
                "incus",
                "config",
                "device",
                "add",
                container,
                "bubble-auth-proxy",
                "proxy",
                f"connect={connect_addr}",
                f"listen={listen_addr}",
                "bind=container",
            ],
            timeout=15,
        )
    except Exception as e:
        if not machine_readable:
            detail(f"Warning: failed to add remote proxy device: {e}")
            detail("No GitHub auth configured (fail-closed).")
        remove_auth_tokens(container)
        return False

    # Configure git inside the container to use the proxy
    q_token = shlex.quote(token)
    git_config_cmd = (
        f"git config --global url.'http://127.0.0.1:{_CONTAINER_PROXY_PORT}/git/'.insteadOf"
        f" 'https://github.com/'"
        f" && git config --global http.'http://127.0.0.1:{_CONTAINER_PROXY_PORT}/'.extraHeader"
        f" 'X-Bubble-Token: {q_token}'"
    )

    try:
        _ssh_run(
            remote_host,
            [
                "incus",
                "exec",
                container,
                "--",
                "bash",
                "-c",
                f"su - user -c {shlex.quote(git_config_cmd)}",
            ],
            timeout=15,
        )
    except Exception as e:
        if not machine_readable:
            detail(f"Warning: failed to configure git proxy on remote: {e}")
            detail("No GitHub auth configured (fail-closed).")
        remove_auth_tokens(container)
        return False

    if not machine_readable:
        detail(f"GitHub auth proxy configured (scoped to {owner}/{repo}, via SSH tunnel).")
    return True


def setup_gh_token(
    runtime: ContainerRuntime,
    container: str,
    owner: str = "",
    repo: str = "",
    machine_readable: bool = False,
    remote_host=None,
) -> bool:
    """Set up GitHub auth for a container.

    For local containers: uses the auth proxy via Incus proxy device.
    For remote/cloud containers: tunnels the auth proxy via SSH -R.

    Both paths provide repo-scoped auth — the host token never enters
    the container.

    Returns True if auth was successfully configured.
    """
    if not owner or not repo:
        if not machine_readable:
            detail("Warning: no owner/repo available, cannot set up scoped auth.")
        return False

    if remote_host:
        return setup_auth_proxy_remote(remote_host, container, owner, repo, machine_readable)

    if runtime:
        return setup_auth_proxy(runtime, container, owner, repo, machine_readable)

    return False
