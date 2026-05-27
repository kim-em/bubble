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

import json
import platform
import shlex
import subprocess

from .output import detail
from .runtime.base import ContainerRuntime
from .runtime.colima import colima_bind_ip

# Port inside the container where the auth proxy is exposed (legacy
# proxy-device flow only — TCP, for git).
_CONTAINER_PROXY_PORT = 7654

# Unix socket path inside the container for gh CLI access (legacy
# proxy-device flow only).
_CONTAINER_GH_SOCKET = "/bubble/gh-proxy.sock"

# Bridge-listener flow: per-bubble mount point exposing the host's
# proxy-sockets directory. Created by the disk device we add at
# bubble-open time; the gh tool script points at <this>/gh.sock.
_CONTAINER_PROXY_MOUNT = "/run/bubble-proxy"
_CONTAINER_GH_SOCKET_BRIDGE = f"{_CONTAINER_PROXY_MOUNT}/gh.sock"


# ---------------------------------------------------------------------------
# Sensitive-payload helpers
# ---------------------------------------------------------------------------
#
# Tokens MUST NOT appear in any process's argv on the host or inside the
# container — argv is visible via /proc/<pid>/cmdline to other users on the
# box.  These helpers build bash snippets that read the token from stdin
# (we pipe it via the runtime's input=... channel) and use heredoc-fed
# `cat` to write config files; both avoid putting the token on a command
# line.
#
# The bash text itself only references the *literal* string ``$TOKEN`` —
# it's the variable name, not the value, so it is safe to ship in argv.


def _bash_with_stdin_token(payload: str) -> str:
    """Wrap *payload* so it runs with ``$TOKEN`` set from stdin.

    The caller passes the script via stdin (see ``runtime.exec(input=...)``
    or ``_ssh_run(input=...)``).  The first line of stdin is read into
    ``TOKEN``; *payload* may reference ``$TOKEN`` from that point on.

    ``set -e`` aborts on the first failing step so a partial config write
    doesn't leave a half-configured container.
    """
    # `IFS= read -r` reads one line without trimming; this is safer than
    # `cat` which would slurp any trailing newline-or-content the caller
    # might have appended.
    return f"set -e\nIFS= read -r TOKEN\n{payload}"


# Append [url] and [http] sections to user's .gitconfig.  Heredoc body
# expansion happens in bash before piping to cat — no argv exposure.
# {port} is the only template substitution; $TOKEN is literal until bash
# expands it inside the heredoc.
_AUTH_PROXY_GIT_CONFIG_PAYLOAD = """\
mkdir -p /home/user
touch /home/user/.gitconfig
chown user:user /home/user/.gitconfig
chmod 600 /home/user/.gitconfig
cat >> /home/user/.gitconfig <<GITCONFIG
[url "http://127.0.0.1:{port}/git/"]
\tinsteadOf = https://github.com/
[http "http://127.0.0.1:{port}/"]
\textraHeader = X-Bubble-Token: $TOKEN
GITCONFIG
"""


# Same as above but uses an arbitrary host:port endpoint — used by the
# bridge-listener flow where the container reaches the daemon directly
# via the incus bridge IP (no per-bubble proxy device).
_AUTH_PROXY_GIT_CONFIG_BRIDGE_PAYLOAD = """\
mkdir -p /home/user
touch /home/user/.gitconfig
chown user:user /home/user/.gitconfig
chmod 600 /home/user/.gitconfig
cat >> /home/user/.gitconfig <<GITCONFIG
[url "http://{endpoint}/git/"]
\tinsteadOf = https://github.com/
[http "http://{endpoint}/"]
\textraHeader = X-Bubble-Token: $TOKEN
GITCONFIG
"""


# Write /etc/profile.d/bubble-gh.sh with GH_CONFIG_DIR / GH_TOKEN / GH_REPO.
# Both GH_REPO and GH_REPO_FILE are template-only (template-injected before
# bash sees them) — they're shell metadata-free identifiers.  Only $TOKEN
# is read from stdin.
_GH_PROXY_PROFILE_PAYLOAD = """\
mkdir -p /etc/profile.d
cat > /etc/profile.d/bubble-gh.sh <<PROFILE
export GH_CONFIG_DIR=/etc/bubble/gh
export GH_TOKEN=$TOKEN{gh_repo_line}
PROFILE
chmod 644 /etc/profile.d/bubble-gh.sh{repo_file_block}
"""


# Write /etc/profile.d/bubble-gh-inject.sh with GH_TOKEN and GITHUB_TOKEN.
# Used by the `direct` (level 5) escape hatch — the host's real token goes
# into the container's environment.
_GH_INJECT_PROFILE_PAYLOAD = """\
mkdir -p /etc/profile.d
cat > /etc/profile.d/bubble-gh-inject.sh <<PROFILE
export GH_TOKEN=$TOKEN
export GITHUB_TOKEN=$TOKEN
PROFILE
chmod 644 /etc/profile.d/bubble-gh-inject.sh
"""


def _gh_proxy_profile_payload(owner: str, repo: str) -> str:
    """Build the gh proxy profile.d payload.  The optional GH_REPO line
    only appears when owner/repo are known."""
    if owner and repo:
        # owner/repo are repo identifiers and contain no shell metacharacters
        # we'd worry about — but quote anyway for defense in depth.
        q_repo = shlex.quote(f"{owner}/{repo}")
        gh_repo_line = f"\nexport GH_REPO={q_repo}"
        repo_file_block = f"\nmkdir -p /etc/bubble/gh && echo {q_repo} > /etc/bubble/gh/repo"
    else:
        gh_repo_line = ""
        repo_file_block = ""
    return _GH_PROXY_PROFILE_PAYLOAD.format(
        gh_repo_line=gh_repo_line,
        repo_file_block=repo_file_block,
    )


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

    This is the **legacy** signature kept for backwards-compat with
    tests and the remote/cloud auth-setup paths. New code should call
    :func:`_ensure_auth_proxy_endpoint` instead, which also reports the
    Unix-socket listener path used by the bridge flow.
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


def _ensure_auth_proxy_endpoint() -> dict | None:
    """Ensure the daemon is running and return its endpoint metadata.

    Returns a dict like::

        {
          "tcp": {"host": "10.156.104.1", "port": 7654},
          "unix_socket": "/home/kim/.bubble/proxy-sockets/gh.sock",
          "version": 2,
        }

    or ``None`` if the daemon failed to start or wrote only the legacy
    ``auth-proxy.port`` file. In the legacy-file case callers fall
    back to the proxy-device flow.
    """
    from .auth_proxy import AUTH_PROXY_ENDPOINT_FILE
    from .automation import install_auth_proxy_daemon, is_auth_proxy_installed

    if not is_auth_proxy_installed():
        install_auth_proxy_daemon()

    import time

    for _ in range(10):
        if AUTH_PROXY_ENDPOINT_FILE.exists():
            try:
                return json.loads(AUTH_PROXY_ENDPOINT_FILE.read_text())
            except (json.JSONDecodeError, OSError):
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

    Two flows depending on what the daemon advertises:

    * **Bridge flow** (preferred): daemon advertises a TCP endpoint on
      the incus bridge IP plus a Unix-socket path. We bind-mount the
      host's socket directory into the bubble via a ``disk`` device,
      and configure git to talk to the bridge URL directly. **No
      ``proxy``-type devices** — those are what leak forkproxy helpers
      on stop/start cycles.
    * **Legacy flow**: daemon advertises only ``auth-proxy.port``. We
      add the old ``bubble-auth-proxy`` / ``bubble-gh-proxy`` incus
      proxy devices and configure git for ``127.0.0.1``. Used for
      remote bubbles (where the SSH-tunnel side needs an extra hop).

    Returns True if setup succeeded.
    """

    rest_api = _resolve_rest_api(config or {}, gh_enabled)
    graphql_read, graphql_write = _resolve_graphql_config(config or {}, gh_enabled)

    endpoint = _ensure_auth_proxy_endpoint()
    if not endpoint:
        # Fall through to the legacy path if the daemon hasn't written
        # the v2 endpoint file (older daemon binary, partial install).
        port = _ensure_auth_proxy_running()
        if not port:
            if not machine_readable:
                detail("Warning: auth proxy failed to start. No GitHub auth configured.")
                detail("Run 'bubble gh proxy start' to diagnose.")
            return False
        return _setup_auth_proxy_legacy(
            runtime,
            container,
            owner,
            repo,
            port,
            rest_api,
            graphql_read,
            graphql_write,
            machine_readable,
            gh_enabled,
        )

    return _setup_auth_proxy_bridge(
        runtime,
        container,
        owner,
        repo,
        endpoint,
        rest_api,
        graphql_read,
        graphql_write,
        machine_readable,
        gh_enabled,
    )


def _setup_auth_proxy_bridge(
    runtime: ContainerRuntime,
    container: str,
    owner: str,
    repo: str,
    endpoint: dict,
    rest_api: bool,
    graphql_read: str,
    graphql_write: str,
    machine_readable: bool,
    gh_enabled: bool,
) -> bool:
    """Bridge-listener setup: no proxy-type devices, just a disk mount
    plus per-bubble token issued with the container's IP baked in."""
    from .auth_proxy import generate_auth_token

    tcp = endpoint.get("tcp") or {}
    host_ip = tcp.get("host")
    port = tcp.get("port")
    unix_socket = endpoint.get("unix_socket")
    if not host_ip or not port:
        if not machine_readable:
            detail("Warning: auth proxy endpoint file missing TCP info; falling back.")
        return False

    # Container IP binds the token to a specific source. Incus
    # security.ipv4_filtering on the bubble's NIC makes this trustable;
    # without it, the bind still happens but a hostile bubble could
    # spoof IPs to use someone else's token. We enforce the filtering
    # at bubble create time (see cli provisioning path).
    container_ip = runtime.container_ipv4(container)

    token = generate_auth_token(
        container,
        owner,
        repo,
        rest_api=rest_api,
        graphql_read=graphql_read,
        graphql_write=graphql_write,
        container_ip=container_ip,
    )

    # Mount the host's proxy-sockets dir into the bubble so gh can
    # reach the Unix socket without a proxy device. The mount is a
    # `disk`-type device (no forkproxy helper).
    if gh_enabled and rest_api and unix_socket:
        import os.path

        socket_dir = os.path.dirname(unix_socket)
        try:
            runtime.add_disk(
                container,
                "bubble-proxy-sockets",
                source=socket_dir,
                path=_CONTAINER_PROXY_MOUNT,
            )
        except Exception as e:
            if not machine_readable:
                detail(f"Warning: failed to mount proxy socket dir: {e}")
                detail("gh CLI will not have API access; git will still work.")
            # Don't fail — git via TCP still works without the gh mount.

    # Configure git: talk to the bridge TCP endpoint directly.
    endpoint_str = f"{host_ip}:{port}"
    payload = _AUTH_PROXY_GIT_CONFIG_BRIDGE_PAYLOAD.format(endpoint=endpoint_str)
    try:
        runtime.exec(
            container,
            ["bash", "-c", _bash_with_stdin_token(payload)],
            input=token + "\n",
        )
    except RuntimeError as e:
        if not machine_readable:
            detail(f"Warning: failed to configure git proxy: {e}")
            detail("No GitHub auth configured (fail-closed).")
        return False

    if gh_enabled and rest_api:
        _setup_gh_proxy_bridge(runtime, container, token, machine_readable, owner, repo)

    if not machine_readable:
        mode_desc = _describe_graphql_mode(graphql_read, graphql_write)
        detail(
            f"GitHub auth proxy configured via bridge {endpoint_str} "
            f"(scoped to {owner}/{repo}, {mode_desc})."
        )
    return True


def _setup_auth_proxy_legacy(
    runtime: ContainerRuntime,
    container: str,
    owner: str,
    repo: str,
    port: int,
    rest_api: bool,
    graphql_read: str,
    graphql_write: str,
    machine_readable: bool,
    gh_enabled: bool,
) -> bool:
    """Legacy setup that adds the bubble-auth-proxy / bubble-gh-proxy
    incus proxy devices. Kept for compatibility with older daemons and
    remote bubbles where the bridge listener isn't reachable."""
    from .auth_proxy import generate_auth_token

    token = generate_auth_token(
        container,
        owner,
        repo,
        rest_api=rest_api,
        graphql_read=graphql_read,
        graphql_write=graphql_write,
    )

    if platform.system() == "Darwin":
        host_ip = colima_bind_ip()
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

    _wait_for_proxy_device(runtime, container, _CONTAINER_PROXY_PORT)

    payload = _AUTH_PROXY_GIT_CONFIG_PAYLOAD.format(port=_CONTAINER_PROXY_PORT)
    try:
        runtime.exec(
            container,
            ["bash", "-c", _bash_with_stdin_token(payload)],
            input=token + "\n",
        )
    except RuntimeError as e:
        if not machine_readable:
            detail(f"Warning: failed to configure git proxy: {e}")
            detail("No GitHub auth configured (fail-closed).")
        return False

    if gh_enabled and rest_api:
        _setup_gh_proxy(runtime, container, token, connect_addr, machine_readable, owner, repo)

    if not machine_readable:
        mode_desc = _describe_graphql_mode(graphql_read, graphql_write)
        detail(f"GitHub auth proxy configured (scoped to {owner}/{repo}, {mode_desc}).")
    return True


def _setup_gh_proxy_bridge(
    runtime: ContainerRuntime,
    container: str,
    token: str,
    machine_readable: bool,
    owner: str = "",
    repo: str = "",
):
    """Configure ``gh`` to use the bind-mounted Unix socket.

    Mirrors :func:`_setup_gh_proxy` but writes a different
    ``http_unix_socket`` path (the bind-mount, not the incus proxy
    device path) and lets the disk device added by
    :func:`_setup_auth_proxy_bridge` provide the socket.
    """
    # Override gh's image-baked config to point at the mounted socket.
    # The token is written via the same stdin-piped heredoc so it
    # doesn't appear in argv.
    payload = _gh_proxy_profile_payload(owner, repo) + (
        "\nmkdir -p /etc/bubble/gh"
        "\ncat > /etc/bubble/gh/config.yml <<GHCONF\n"
        'version: "1"\n'
        f"http_unix_socket: {_CONTAINER_GH_SOCKET_BRIDGE}\n"
        "GHCONF\n"
        "chown 1001:1001 /etc/bubble/gh/config.yml\n"
    )
    try:
        runtime.exec(
            container,
            ["bash", "-c", _bash_with_stdin_token(payload)],
            input=token + "\n",
        )
    except RuntimeError as e:
        if not machine_readable:
            detail(f"Warning: failed to configure gh environment: {e}")


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
    # GH_REPO tells gh which repo to use, bypassing remote URL parsing.
    payload = _gh_proxy_profile_payload(owner, repo)
    try:
        runtime.exec(
            container,
            ["bash", "-c", _bash_with_stdin_token(payload)],
            input=token + "\n",
        )
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
                "bubble",
                "internal",
                "incus-add-device",
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

    # Configure git inside the container to use the proxy.  Token comes
    # via stdin (--with-stdin) so it never appears in the remote argv.
    payload = _AUTH_PROXY_GIT_CONFIG_PAYLOAD.format(port=_CONTAINER_PROXY_PORT)
    try:
        _ssh_run(
            remote_host,
            [
                "bubble",
                "internal",
                "incus-exec",
                "--with-stdin",
                container,
                "bash",
                "-c",
                _bash_with_stdin_token(payload),
            ],
            timeout=15,
            input=token + "\n",
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
                "bubble",
                "internal",
                "incus-add-device",
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

    # Configure gh environment via profile.d.  Token comes via stdin.
    payload = _gh_proxy_profile_payload(owner, repo)
    try:
        _ssh_run(
            remote_host,
            [
                "bubble",
                "internal",
                "incus-exec",
                "--with-stdin",
                container,
                "bash",
                "-c",
                _bash_with_stdin_token(payload),
            ],
            timeout=15,
            input=token + "\n",
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

    try:
        runtime.exec(
            container,
            ["bash", "-c", _bash_with_stdin_token(_GH_INJECT_PROFILE_PAYLOAD)],
            input=token + "\n",
        )
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

    try:
        _ssh_run(
            remote_host,
            [
                "bubble",
                "internal",
                "incus-exec",
                "--with-stdin",
                container,
                "bash",
                "-c",
                _bash_with_stdin_token(_GH_INJECT_PROFILE_PAYLOAD),
            ],
            timeout=15,
            input=token + "\n",
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
