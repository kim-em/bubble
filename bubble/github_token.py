"""GitHub authentication for containers via auth proxy.

Instead of injecting the host's full GitHub token into containers,
we run an HTTP reverse proxy on the host that:
1. Receives plain HTTP git requests from the container
2. Validates the request targets only the allowed repository
3. Adds the real Authorization header
4. Forwards to GitHub over HTTPS

The host GitHub token never enters the container. Each container
gets a per-container bearer token scoped to one repository.

For remote/cloud bubbles where the auth proxy isn't available,
falls back to direct token injection (the old behavior).
"""

import platform
import shlex
import subprocess
import tempfile
from pathlib import Path

import click

from .runtime.base import ContainerRuntime

# Path inside the container where the token is temporarily stored (fallback only).
_CONTAINER_TOKEN_PATH = "/tmp/.gh-token"

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
            click.echo("  Warning: auth proxy failed to start. No GitHub auth configured.")
            click.echo("  Run 'bubble gh proxy start' to diagnose.")
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
            click.echo(f"  Warning: failed to add proxy device: {e}")
            click.echo("  No GitHub auth configured (fail-closed).")
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
            click.echo(f"  Warning: failed to configure git proxy: {e}")
            click.echo("  No GitHub auth configured (fail-closed).")
        return False

    if not machine_readable:
        click.echo(f"  GitHub auth proxy configured (scoped to {owner}/{repo}).")
    return True


def inject_gh_token(runtime: ContainerRuntime, container: str, token: str) -> bool:
    """Inject a GitHub token into a container via gh auth login.

    Writes the token to a temp file on the host, pushes it into the
    container, then has the container consume and delete it. The token
    never appears in process arguments.

    Returns True if injection succeeded.
    """
    # Write token to a host-side temp file (mode 0600)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".gh-token", delete=False) as f:
        f.write(token + "\n")
        tmp_path = f.name
    try:
        Path(tmp_path).chmod(0o600)
        runtime.push_file(container, tmp_path, _CONTAINER_TOKEN_PATH)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Inside the container: consume the token file, authenticate, delete it.
    try:
        runtime.exec(
            container,
            [
                "bash",
                "-c",
                f"chmod 600 {_CONTAINER_TOKEN_PATH}"
                f" && chown user:user {_CONTAINER_TOKEN_PATH}"
                f" && su - user -c '"
                f"gh auth login --with-token < {_CONTAINER_TOKEN_PATH}"
                f" && gh auth setup-git"
                f"'"
                f" ; rm -f {_CONTAINER_TOKEN_PATH}",
            ],
        )
        return True
    except RuntimeError:
        # Clean up the token file even on failure
        try:
            runtime.exec(container, ["rm", "-f", _CONTAINER_TOKEN_PATH])
        except RuntimeError:
            pass
        return False


def inject_gh_token_remote(remote_host, container: str, token: str) -> bool:
    """Inject a GitHub token into a container on a remote host.

    Pipes the token via stdin through SSH to avoid it appearing in
    process arguments on either the local or remote host.

    Returns True if injection succeeded.
    """
    import shlex as shlex_mod

    # Build the remote command: write stdin to temp file, auth, clean up.
    # The token arrives via SSH stdin, never in argv.
    remote_cmd = (
        f"cat > {_CONTAINER_TOKEN_PATH}"
        f" && incus exec {shlex_mod.quote(container)} --"
        f" bash -c '"
        f"chmod 600 {_CONTAINER_TOKEN_PATH}"
        f" && chown user:user {_CONTAINER_TOKEN_PATH}"
        f' && su - user -c \\"gh auth login --with-token < {_CONTAINER_TOKEN_PATH}'
        f' && gh auth setup-git\\"'
        f" ; rm -f {_CONTAINER_TOKEN_PATH}"
        f"'"
    )
    ssh_cmd = remote_host.ssh_cmd([remote_cmd])
    try:
        result = subprocess.run(
            ssh_cmd,
            input=token + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def setup_gh_token(
    runtime: ContainerRuntime,
    container: str,
    owner: str = "",
    repo: str = "",
    machine_readable: bool = False,
    remote_host=None,
) -> bool:
    """Set up GitHub auth for a container.

    For local containers with owner/repo info: uses the auth proxy
    (repo-scoped, host token never enters the container).

    For remote containers or when owner/repo is unavailable: falls
    back to direct token injection.

    Returns True if auth was successfully configured.
    """
    # Remote containers: direct injection (auth proxy is local-only for now)
    if remote_host:
        token = get_host_gh_token()
        if not token:
            if not machine_readable:
                click.echo("  Warning: gh is not authenticated on host, skipping token injection.")
            return False
        success = inject_gh_token_remote(remote_host, container, token)
        if not machine_readable:
            if success:
                click.echo("  GitHub token injected (remote, full access).")
            else:
                click.echo("  Warning: GitHub token injection failed.")
        return success

    # Local containers with owner/repo: use auth proxy (fail-closed)
    if runtime and owner and repo:
        return setup_auth_proxy(runtime, container, owner, repo, machine_readable)

    # No owner/repo available — can't scope the token
    if not machine_readable:
        click.echo("  Warning: no owner/repo available, cannot set up scoped auth.")
    return False
