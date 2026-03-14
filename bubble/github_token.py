"""GitHub authentication for containers via auth proxy or direct injection.

Most github settings use an HTTP reverse proxy on the host:
1. Receives plain HTTP git requests from the container
2. Validates the request targets only the allowed repository
3. Adds the real Authorization header
4. Forwards to GitHub over HTTPS

The host GitHub token never enters the container. Each container
gets a per-container bearer token scoped to one repository.

The "direct" setting bypasses the proxy entirely: the host's actual
GitHub token is injected into the container as GH_TOKEN and
GITHUB_TOKEN environment variables, giving unrestricted access.

For local containers, the proxy is exposed via Incus proxy devices.
For remote/cloud containers, an SSH reverse tunnel forwards the
local proxy port to the remote host, then an Incus proxy device
on the remote exposes it into the container.

GitHub settings (each a strict superset of the one above):
  off:                      no GitHub access
  basic:                    git push/pull only
  rest:                     + repo-scoped REST API
  allowlist-read-graphql:   + allowlisted GraphQL queries
  allowlist-write-graphql:  + allowlisted GraphQL mutations (default)
  write-graphql:            + arbitrary GraphQL
  direct:                   raw token injection, no proxy

When gh is installed and the github setting includes REST or higher,
the proxy is also exposed as a Unix socket at /bubble/gh-proxy.sock
and gh is configured to route through it via http_unix_socket.
"""

import platform
import shlex
import subprocess

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


def _resolve_rest_api(config: dict, gh_enabled: bool) -> bool:
    """Determine whether REST API access is enabled for a container.

    Returns True if REST API access should be allowed, based on the
    unified github security level and tool availability.  GraphQL
    policies are resolved separately by _resolve_graphql_config().
    """
    from .security import get_github_level

    level = get_github_level(config)

    # basic = git only; off/direct shouldn't reach here but return git-only
    if level in ("off", "basic", "direct"):
        return False

    if not gh_enabled:
        return False

    # rest, allowlist-read-graphql, allowlist-write-graphql, write-graphql
    # REST is already repo-scoped by path validation, so read-write is
    # safe by default.  This enables REST POST operations like
    # gh run rerun (/repos/{owner}/{repo}/actions/runs/{id}/rerun).
    return True


def _resolve_graphql_config(config: dict, gh_enabled: bool) -> tuple[str, str]:
    """Determine GraphQL policies for a container.

    Returns (graphql_read, graphql_write) based on the unified github
    security level.
    """
    from .security import get_github_level

    level = get_github_level(config)

    if not gh_enabled or level in ("off", "basic", "rest", "direct"):
        return "none", "none"

    if level == "allowlist-read-graphql":
        return "whitelisted", "none"

    if level == "allowlist-write-graphql":
        return "whitelisted", "whitelisted"

    if level == "write-graphql":
        return "unrestricted", "unrestricted"

    return "none", "none"


def _describe_graphql_mode(graphql_read: str, graphql_write: str) -> str:
    """Human-readable description of GraphQL access mode."""
    if graphql_read == "whitelisted" and graphql_write == "whitelisted":
        return "repo-scoped (allowlisted GraphQL)"
    if graphql_read == "unrestricted" and graphql_write == "unrestricted":
        return "unrestricted GraphQL read-write"
    if graphql_read == "unrestricted" and graphql_write == "none":
        return "unrestricted GraphQL read-only"
    if graphql_read == "none" and graphql_write == "none":
        return "git only"
    return f"GraphQL read={graphql_read}, write={graphql_write}"


def _wait_for_proxy_device(runtime: ContainerRuntime, container: str, port: int):
    """Wait for a proxy device TCP listener to be ready inside a container.

    Incus proxy devices may take a brief moment after add_device returns
    before the TCP listener is actually accepting connections.  Does a
    single retry-loop inside one ``incus exec`` call to avoid per-attempt
    subprocess overhead.
    """
    try:
        runtime.exec(
            container,
            [
                "bash",
                "-c",
                f"for i in $(seq 1 20); do"
                f" (echo > /dev/tcp/127.0.0.1/{port}) 2>/dev/null && exit 0;"
                f" sleep 0.1; done; exit 1",
            ],
        )
    except RuntimeError:
        pass  # Best-effort; clone will produce a clearer error if still down


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

    rest_api = _resolve_rest_api(config or {}, gh_enabled)
    graphql_read, graphql_write = _resolve_graphql_config(config or {}, gh_enabled)

    port = _ensure_auth_proxy_running()
    if not port:
        if not machine_readable:
            detail("Warning: auth proxy failed to start. No GitHub auth configured.")
            detail("Run 'bubble gh proxy start' to diagnose.")
        return False

    # Generate per-container token with appropriate access policy
    token = generate_auth_token(
        container,
        owner,
        repo,
        rest_api=rest_api,
        graphql_read=graphql_read,
        graphql_write=graphql_write,
    )

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

    # Wait for the Incus proxy device TCP listener to be ready inside the
    # container.  There's a small delay between add_device returning and
    # the listener actually accepting connections.
    _wait_for_proxy_device(runtime, container, _CONTAINER_PROXY_PORT)

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
    if gh_enabled and rest_api:
        _setup_gh_proxy(runtime, container, token, connect_addr, machine_readable, owner, repo)

    if not machine_readable:
        mode_desc = _describe_graphql_mode(graphql_read, graphql_write)
        detail(f"GitHub auth proxy configured (scoped to {owner}/{repo}, {mode_desc}).")
    return True


def _setup_gh_proxy(
    runtime: ContainerRuntime,
    container: str,
    token: str,
    connect_addr: str,
    machine_readable: bool,
    owner: str = "",
    repo: str = "",
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
            uid="1001",
            gid="1001",
            mode="0660",
        )
    except Exception as e:
        if not machine_readable:
            detail(f"Warning: failed to add gh proxy device: {e}")
            detail("gh CLI will not have API access.")
        return

    # Configure GH_CONFIG_DIR and GH_TOKEN in the container via profile.d.
    # GH_CONFIG_DIR points to /etc/bubble/gh/ (created by gh.sh tool script)
    # GH_TOKEN is the per-container bubble proxy token — gh sends it as
    # Authorization header, the proxy validates it and swaps in the real token.
    q_token = shlex.quote(token)
    # GH_REPO tells gh which repo to use, bypassing remote URL parsing.
    # Without this, gh can't match the proxy URL (127.0.0.1:7654) to github.com.
    gh_repo_line = ""
    if owner and repo:
        q_repo = shlex.quote(f"{owner}/{repo}")
        gh_repo_line = f" && echo 'export GH_REPO={q_repo}' >> /etc/profile.d/bubble-gh.sh"
    profile_script = (
        f'echo "export GH_CONFIG_DIR=/etc/bubble/gh" > /etc/profile.d/bubble-gh.sh'
        f" && echo 'export GH_TOKEN={q_token}' >> /etc/profile.d/bubble-gh.sh"
        f"{gh_repo_line}"
        f" && chmod 644 /etc/profile.d/bubble-gh.sh"
    )

    try:
        runtime.exec(container, ["bash", "-c", profile_script])
    except RuntimeError as e:
        if not machine_readable:
            detail(f"Warning: failed to configure gh environment: {e}")


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

    rest_api = _resolve_rest_api(config or {}, gh_enabled)
    graphql_read, graphql_write = _resolve_graphql_config(config or {}, gh_enabled)

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

    # Generate per-container token with appropriate access policy
    token = generate_auth_token(
        container,
        owner,
        repo,
        rest_api=rest_api,
        graphql_read=graphql_read,
        graphql_write=graphql_write,
    )

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
    if gh_enabled and rest_api:
        _setup_gh_proxy_remote(
            remote_host, container, token, connect_addr, machine_readable, owner, repo
        )

    if not machine_readable:
        mode_desc = _describe_graphql_mode(graphql_read, graphql_write)
        detail(
            f"GitHub auth proxy configured (scoped to {owner}/{repo}, {mode_desc}, via SSH tunnel)."
        )
    return True


def _setup_gh_proxy_remote(
    remote_host,
    container: str,
    token: str,
    connect_addr: str,
    machine_readable: bool,
    owner: str = "",
    repo: str = "",
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
                "uid=1001",
                "gid=1001",
                "mode=0660",
            ],
            timeout=15,
        )
    except Exception as e:
        if not machine_readable:
            detail(f"Warning: failed to add gh proxy device on remote: {e}")
            detail("gh CLI will not have API access.")
        return

    # Configure gh environment via profile.d
    q_token = shlex.quote(token)
    gh_repo_line = ""
    if owner and repo:
        q_repo = shlex.quote(f"{owner}/{repo}")
        gh_repo_line = f" && echo 'export GH_REPO={q_repo}' >> /etc/profile.d/bubble-gh.sh"
    profile_cmd = (
        f'echo "export GH_CONFIG_DIR=/etc/bubble/gh" > /etc/profile.d/bubble-gh.sh'
        f" && echo 'export GH_TOKEN={q_token}' >> /etc/profile.d/bubble-gh.sh"
        f"{gh_repo_line}"
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
            detail(f"Warning: failed to configure gh environment on remote: {e}")


def inject_gh_token(
    runtime: ContainerRuntime,
    container: str,
    machine_readable: bool = False,
) -> bool:
    """Inject the host's GitHub token directly into a local container.

    Sets GH_TOKEN and GITHUB_TOKEN environment variables via /etc/profile.d
    so both gh CLI and git credential helpers have full access.

    This is the level 5 escape hatch — the real token is inside the container.

    Returns True if injection succeeded.
    """
    token = get_host_gh_token()
    if not token:
        if not machine_readable:
            detail("Warning: no host GitHub token available. Run 'gh auth login' first.")
        return False

    q_token = shlex.quote(token)
    profile_script = (
        f'echo "export GH_TOKEN={q_token}" > /etc/profile.d/bubble-gh-inject.sh'
        f" && echo 'export GITHUB_TOKEN={q_token}' >> /etc/profile.d/bubble-gh-inject.sh"
        f" && chmod 644 /etc/profile.d/bubble-gh-inject.sh"
    )

    try:
        runtime.exec(container, ["bash", "-c", profile_script])
    except RuntimeError as e:
        if not machine_readable:
            detail(f"Warning: failed to inject GitHub token: {e}")
        return False

    if not machine_readable:
        detail("GitHub token injected directly (level 5: unrestricted access).")
    return True


def inject_gh_token_remote(
    remote_host,
    container: str,
    machine_readable: bool = False,
) -> bool:
    """Inject the host's GitHub token directly into a remote container.

    Same as inject_gh_token but operates on a remote host via SSH.

    Returns True if injection succeeded.
    """
    from .remote import _ssh_run

    token = get_host_gh_token()
    if not token:
        if not machine_readable:
            detail("Warning: no host GitHub token available. Run 'gh auth login' first.")
        return False

    q_token = shlex.quote(token)
    profile_script = (
        f'echo "export GH_TOKEN={q_token}" > /etc/profile.d/bubble-gh-inject.sh'
        f" && echo 'export GITHUB_TOKEN={q_token}' >> /etc/profile.d/bubble-gh-inject.sh"
        f" && chmod 644 /etc/profile.d/bubble-gh-inject.sh"
    )

    try:
        _ssh_run(
            remote_host,
            ["incus", "exec", container, "--", "bash", "-c", profile_script],
            timeout=15,
        )
    except Exception as e:
        if not machine_readable:
            detail(f"Warning: failed to inject GitHub token on remote: {e}")
        return False

    if not machine_readable:
        detail("GitHub token injected directly (level 5: unrestricted access).")
    return True


def setup_gh_token(
    runtime: ContainerRuntime,
    container: str,
    owner: str = "",
    repo: str = "",
    machine_readable: bool = False,
    remote_host=None,
    gh_enabled: bool = False,
    config: dict | None = None,
    token_inject: bool = False,
) -> bool:
    """Set up GitHub auth for a container.

    When token_inject is True (level 5), the host's actual GitHub token
    is injected directly into the container, bypassing the proxy.

    Otherwise, for local containers: uses the auth proxy via Incus proxy device.
    For remote/cloud containers: tunnels the auth proxy via SSH -R.

    Both proxy paths provide repo-scoped auth — the host token never enters
    the container.

    When gh_enabled is True and the github security level includes API access, also sets up
    gh CLI access via Unix socket proxy device and configures gh to use
    the auth proxy.

    Returns True if auth was successfully configured.
    """
    # Level 5: direct token injection (bypasses proxy entirely)
    if token_inject:
        if remote_host:
            return inject_gh_token_remote(remote_host, container, machine_readable)
        if runtime:
            return inject_gh_token(runtime, container, machine_readable)
        return False

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
