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

Access levels:
  Level 1: git only (push/pull)
  Level 3: git + gh read-only (REST read + GraphQL queries)
  Level 4: git + gh read-write (REST read-write + GraphQL mutations)

When the gh tool is installed and github_api is enabled, the proxy
is also exposed as a Unix socket at /bubble/gh-proxy.sock and gh
is configured to route through it via http_unix_socket.
"""

import platform
import shlex
import subprocess

import click

from .output import detail
from .runtime.base import ContainerRuntime
from .runtime.colima import colima_host_ip

# Port inside the container where the auth proxy is exposed (TCP, for git)
_CONTAINER_PROXY_PORT = 7654

# Unix socket path inside the container for gh CLI access
_CONTAINER_GH_SOCKET = "/bubble/gh-proxy.sock"


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


def _resolve_access_level(config: dict, gh_enabled: bool) -> int:
    """Determine the auth proxy access level for a container.

    Returns the access level (1-4) based on config and tool availability.
    """
    from .auth_proxy import DEFAULT_LEVEL, LEVEL_GH_READWRITE, LEVEL_GIT_ONLY
    from .security import get_setting, is_enabled

    if not gh_enabled or not is_enabled(config, "github_api"):
        return LEVEL_GIT_ONLY

    if get_setting(config, "github_api") == "read-write":
        return LEVEL_GH_READWRITE

    return DEFAULT_LEVEL  # LEVEL_GH_READ (3)


def setup_auth_proxy(
    runtime: ContainerRuntime,
    container: str,
    owner: str,
    repo: str,
    machine_readable: bool = False,
    gh_enabled: bool = False,
    config: dict | None = None,
) -> bool:
    """Set up auth proxy access for a container.

    1. Ensure auth proxy daemon is running
    2. Generate per-container token scoped to owner/repo
    3. Add Incus proxy device exposing the proxy into the container
    4. Configure git inside the container to use the proxy
    5. If gh is enabled: add Unix socket proxy device and configure gh

    Returns True if setup succeeded.
    """
    from .auth_proxy import generate_auth_token

    level = _resolve_access_level(config or {}, gh_enabled)

    port = _ensure_auth_proxy_running()
    if not port:
        if not machine_readable:
            detail("Warning: auth proxy failed to start. No GitHub auth configured.")
            detail("Run 'bubble gh proxy start' to diagnose.")
        return False

    # Generate per-container token with appropriate access level
    token = generate_auth_token(container, owner, repo, level=level)

    # Add Incus proxy device: expose host TCP port into container
    # On macOS (Colima), need to use the host IP from the VM's perspective
    if platform.system() == "Darwin":
        host_ip = colima_host_ip()
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

    # Set up gh CLI access via Unix socket proxy device
    if gh_enabled and level >= 2:
        _setup_gh_proxy(runtime, container, token, connect_addr, machine_readable)

    if not machine_readable:
        level_desc = {1: "git only", 2: "REST read-only", 3: "gh read-only", 4: "gh read-write"}
        detail(
            f"GitHub auth proxy configured"
            f" (scoped to {owner}/{repo}, level {level}: {level_desc.get(level, '?')})."
        )
    return True


def _setup_gh_proxy(
    runtime: ContainerRuntime,
    container: str,
    token: str,
    connect_addr: str,
    machine_readable: bool,
):
    """Set up gh CLI access via Unix socket proxy device.

    Adds a second Incus proxy device that exposes the auth proxy as a
    Unix socket inside the container, and configures gh to use it via
    GH_CONFIG_DIR and GH_TOKEN environment variables.
    """
    # Add Unix socket proxy device for gh
    try:
        runtime.add_device(
            container,
            "bubble-gh-proxy",
            "proxy",
            connect=connect_addr,
            listen=f"unix:{_CONTAINER_GH_SOCKET}",
            bind="container",
            uid="1000",
            gid="1000",
            mode="0660",
        )
    except Exception as e:
        if not machine_readable:
            click.echo(f"  Warning: failed to add gh proxy device: {e}")
            click.echo("  gh CLI will not have API access.")
        return

    # Configure GH_CONFIG_DIR and GH_TOKEN in the container via profile.d.
    # GH_CONFIG_DIR points to /etc/bubble/gh/ (created by gh.sh tool script)
    # GH_TOKEN is the per-container bubble proxy token — gh sends it as
    # Authorization header, the proxy validates it and swaps in the real token.
    q_token = shlex.quote(token)
    profile_script = (
        f'echo "export GH_CONFIG_DIR=/etc/bubble/gh" > /etc/profile.d/bubble-gh.sh'
        f" && echo 'export GH_TOKEN={q_token}' >> /etc/profile.d/bubble-gh.sh"
        f" && chmod 644 /etc/profile.d/bubble-gh.sh"
    )

    try:
        runtime.exec(container, ["bash", "-c", profile_script])
    except RuntimeError as e:
        if not machine_readable:
            click.echo(f"  Warning: failed to configure gh environment: {e}")


def setup_auth_proxy_remote(
    remote_host,
    container: str,
    owner: str,
    repo: str,
    machine_readable: bool = False,
    gh_enabled: bool = False,
    config: dict | None = None,
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

    level = _resolve_access_level(config or {}, gh_enabled)

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

    # Generate per-container token with appropriate access level
    token = generate_auth_token(container, owner, repo, level=level)

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

    # Set up gh CLI access via Unix socket proxy device on remote
    if gh_enabled and level >= 2:
        _setup_gh_proxy_remote(remote_host, container, token, connect_addr, machine_readable)

    if not machine_readable:
        level_desc = {1: "git only", 2: "REST read-only", 3: "gh read-only", 4: "gh read-write"}
        detail(
            f"GitHub auth proxy configured"
            f" (scoped to {owner}/{repo}, level {level}: {level_desc.get(level, '?')}"
            f", via SSH tunnel)."
        )
    return True


def _setup_gh_proxy_remote(
    remote_host,
    container: str,
    token: str,
    connect_addr: str,
    machine_readable: bool,
):
    """Set up gh CLI access on a remote container via Unix socket proxy device."""
    from .remote import _ssh_run

    # Add Unix socket proxy device for gh on remote
    try:
        _ssh_run(
            remote_host,
            [
                "incus",
                "config",
                "device",
                "add",
                container,
                "bubble-gh-proxy",
                "proxy",
                f"connect={connect_addr}",
                f"listen=unix:{_CONTAINER_GH_SOCKET}",
                "bind=container",
                "uid=1000",
                "gid=1000",
                "mode=0660",
            ],
            timeout=15,
        )
    except Exception as e:
        if not machine_readable:
            click.echo(f"  Warning: failed to add gh proxy device on remote: {e}")
            click.echo("  gh CLI will not have API access.")
        return

    # Configure gh environment via profile.d
    q_token = shlex.quote(token)
    profile_cmd = (
        f'echo "export GH_CONFIG_DIR=/etc/bubble/gh" > /etc/profile.d/bubble-gh.sh'
        f" && echo 'export GH_TOKEN={q_token}' >> /etc/profile.d/bubble-gh.sh"
        f" && chmod 644 /etc/profile.d/bubble-gh.sh"
    )

    try:
        _ssh_run(
            remote_host,
            ["incus", "exec", container, "--", "bash", "-c", profile_cmd],
            timeout=15,
        )
    except RuntimeError as e:
        if not machine_readable:
            click.echo(f"  Warning: failed to configure gh environment on remote: {e}")


def setup_gh_token(
    runtime: ContainerRuntime,
    container: str,
    owner: str = "",
    repo: str = "",
    machine_readable: bool = False,
    remote_host=None,
    gh_enabled: bool = False,
    config: dict | None = None,
) -> bool:
    """Set up GitHub auth for a container.

    For local containers: uses the auth proxy via Incus proxy device.
    For remote/cloud containers: tunnels the auth proxy via SSH -R.

    Both paths provide repo-scoped auth — the host token never enters
    the container.

    When gh_enabled is True and github_api security is on, also sets up
    gh CLI access via Unix socket proxy device and configures gh to use
    the auth proxy.

    Returns True if auth was successfully configured.
    """
    if not owner or not repo:
        if not machine_readable:
            detail("Warning: no owner/repo available, cannot set up scoped auth.")
        return False

    if remote_host:
        return setup_auth_proxy_remote(
            remote_host,
            container,
            owner,
            repo,
            machine_readable,
            gh_enabled=gh_enabled,
            config=config,
        )

    if runtime:
        return setup_auth_proxy(
            runtime,
            container,
            owner,
            repo,
            machine_readable,
            gh_enabled=gh_enabled,
            config=config,
        )

    return False
