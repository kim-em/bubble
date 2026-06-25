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

For local containers (Linux or macOS/Colima), the container reaches the
host auth-proxy daemon directly over the incus bridge IP: git via
``url.insteadOf``, and gh via a small in-container ``socat`` forwarder
(gh wants a Unix socket; the forwarder relays it to the same bridge
TCP endpoint). No incus ``proxy``-type devices are created, so there
are no per-bubble ``forkproxy`` helpers to leak.

For remote/cloud containers, an SSH reverse tunnel forwards the local
proxy port to the remote host, then an Incus proxy device on the remote
exposes it into the container (a separate transport in
``setup_auth_proxy_remote``).

GitHub settings (each a strict superset of the one above):
  off:                      no GitHub access
  basic:                    git push/pull only
  rest:                     + repo-scoped REST API
  allowlist-read-graphql:   + allowlisted GraphQL queries
  allowlist-write-graphql:  + allowlisted GraphQL mutations (default)
  write-graphql:            + arbitrary GraphQL
  direct:                   raw token injection, no proxy
"""

import json
import shlex
import subprocess

from .output import detail
from .runtime.base import ContainerRuntime

# Port inside the container where the auth proxy is exposed (legacy
# proxy-device flow only — TCP, for git).
_CONTAINER_PROXY_PORT = 7654

# Unix socket path inside the container for gh CLI access (legacy
# proxy-device flow only).
_CONTAINER_GH_SOCKET = "/bubble/gh-proxy.sock"

# Bridge-listener flow: gh's local Unix socket inside the container. The
# gh wrapper (installed by the gh tool script) lazily runs a socat
# unix→TCP forwarder on this path, relaying to the bridge endpoint
# recorded at /etc/bubble/gh/bridge. User-owned so the unprivileged
# container user can create/connect it. Must match the path the gh
# wrapper uses.
_CONTAINER_GH_SOCKET_BRIDGE = "/home/user/.bubble/gh.sock"


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

        {"tcp": {"host": "10.156.104.1", "port": 7654}, "version": 3}

    or ``None`` if the daemon isn't actually listening (no endpoint file,
    or a stale endpoint file left behind by a crashed daemon). The
    endpoint is health-checked with a TCP connect before being trusted —
    a stale file must not cause us to configure containers against a dead
    listener. On ``None`` local auth setup fails closed (there is no
    proxy-device fallback).
    """
    from .auth_proxy import AUTH_PROXY_ENDPOINT_FILE
    from .automation import install_auth_proxy_daemon, is_auth_proxy_installed

    if not is_auth_proxy_installed():
        install_auth_proxy_daemon()

    import time

    for _ in range(10):
        endpoint = _read_endpoint_file(AUTH_PROXY_ENDPOINT_FILE)
        if endpoint and _endpoint_alive(endpoint):
            return endpoint
        time.sleep(0.5)

    return None


def _read_endpoint_file(path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _endpoint_alive(endpoint: dict) -> bool:
    """TCP-connect to the endpoint to confirm a live daemon is listening.

    Guards against a stale ``auth-proxy.endpoint`` file from a daemon
    that crashed/was killed: without this we'd configure containers to
    talk to a dead listener and report success.
    """
    import socket as _socket

    tcp = endpoint.get("tcp") or {}
    host, port = tcp.get("host"), tcp.get("port")
    if not host or not isinstance(port, int):
        return False
    try:
        with _socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


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


def setup_auth_proxy(
    runtime: ContainerRuntime,
    container: str,
    owner: str,
    repo: str,
    machine_readable: bool = False,
    gh_enabled: bool = False,
    config: dict | None = None,
    push_repos: list[str] | None = None,
) -> bool:
    """Set up auth proxy access for a local container via the bridge flow.

    The container reaches the host auth-proxy daemon directly over the
    incus bridge (git) and a bind-mounted Unix socket (gh). No
    ``proxy``-type incus devices are created, so there are no per-bubble
    ``forkproxy`` helpers to leak on stop/start cycles.

    ``push_repos`` lists additional ``owner/repo`` forks the container may
    git fetch from and push to (REST/GraphQL stay scoped to owner/repo).

    (Remote/cloud bubbles use :func:`setup_auth_proxy_remote`, a separate
    SSH-tunnelled transport.)

    Returns True if setup succeeded.
    """
    rest_api = _resolve_rest_api(config or {}, gh_enabled)
    graphql_read, graphql_write = _resolve_graphql_config(config or {}, gh_enabled)

    endpoint = _ensure_auth_proxy_endpoint()
    if not endpoint:
        if not machine_readable:
            detail("Warning: auth proxy failed to start. No GitHub auth configured.")
            detail("Run 'bubble gh proxy start' to diagnose.")
        return False
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
        push_repos,
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
    push_repos: list[str] | None = None,
) -> bool:
    """Bridge-listener setup: no proxy-type devices, just a disk mount
    for the gh socket plus a per-bubble bearer token."""
    from .auth_proxy import generate_auth_token

    tcp = endpoint.get("tcp") or {}
    host_ip = tcp.get("host")
    port = tcp.get("port")
    if not host_ip or not port:
        if not machine_readable:
            detail("Warning: auth proxy endpoint file missing TCP info.")
        return False
    # A loopback endpoint is unreachable from inside the container (it's
    # the container's own loopback, not the host). This shouldn't happen
    # — the daemon refuses to bind loopback — but guard fail-closed so we
    # never write a container config that can't possibly connect.
    if host_ip.startswith("127."):
        if not machine_readable:
            detail(f"Warning: auth proxy bound to loopback {host_ip}; unreachable from bubbles.")
            detail("No GitHub auth configured (fail-closed).")
        return False

    # Punch a hole in the container's egress allowlist for the bridge
    # endpoint. The network allowlist is applied at provision time, which
    # runs *before* the auth-proxy daemon has written its endpoint file on
    # a cold start — so apply_network can't have added this rule on the
    # first bubble. Add it here (idempotently) where the endpoint is known
    # and the daemon is up. On restart, reapply_network_after_restart adds
    # it too (the endpoint file exists by then). git AND gh both reach the
    # daemon at this same endpoint (gh via the in-container forwarder), so
    # one rule covers both. If egress can't be opened on a network-
    # restricted bubble, fail closed rather than report a working setup
    # the container can't actually use.
    if not _allow_bridge_egress(runtime, container, host_ip, int(port)):
        if not machine_readable:
            detail("Warning: could not open egress to the auth proxy; the bubble's")
            detail("network allowlist would block it. No GitHub auth configured.")
        return False

    token = generate_auth_token(
        container,
        owner,
        repo,
        rest_api=rest_api,
        graphql_read=graphql_read,
        graphql_write=graphql_write,
        push_repos=push_repos,
    )

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
        _setup_gh_proxy_bridge(
            runtime, container, token, host_ip, int(port), machine_readable, owner, repo
        )

    if not machine_readable:
        mode_desc = _describe_graphql_mode(graphql_read, graphql_write)
        detail(
            f"GitHub auth proxy configured via bridge {endpoint_str} "
            f"(scoped to {owner}/{repo}, {mode_desc})."
        )
        if push_repos:
            detail(f"  git fetch/push also allowed to fork(s): {', '.join(push_repos)}")
    return True


def _allow_bridge_egress(runtime: ContainerRuntime, container: str, ip: str, port: int) -> bool:
    """Ensure the container can reach the bridge auth-proxy endpoint.

    Idempotently inserts an ACCEPT ahead of the allowlist's default
    ``OUTPUT DROP`` policy, then verifies it landed. Returns True when
    the rule is in place OR when there's no iptables to configure (e.g.
    ``--no-network`` bubbles, where egress is unrestricted anyway).
    Returns False only when iptables is present but the rule could not
    be installed — i.e. a restricted bubble that genuinely can't reach
    the proxy, which the caller treats as a fail-closed error.

    The probe distinguishes those cases: ``iptables`` exits 127 when the
    binary is absent (unrestricted → True); otherwise we add and re-check
    with ``-C`` (present → True, still-missing → False).
    """
    rule = f"OUTPUT -d {ip} -p tcp --dport {port} -j ACCEPT"
    script = (
        f"command -v iptables >/dev/null 2>&1 || exit 127\n"
        f"iptables -C {rule} 2>/dev/null && exit 0\n"
        f"iptables -A {rule} 2>/dev/null || true\n"
        f"iptables -C {rule} 2>/dev/null"
    )
    try:
        runtime.exec(container, ["bash", "-c", script])
        return True  # rule present (exit 0)
    except RuntimeError as e:
        # exit 127 => no iptables (unrestricted egress); treat as fine.
        return getattr(e, "returncode", None) == 127


def _setup_gh_proxy_bridge(
    runtime: ContainerRuntime,
    container: str,
    token: str,
    host_ip: str,
    port: int,
    machine_readable: bool,
    owner: str = "",
    repo: str = "",
):
    """Configure ``gh`` to reach the bridge daemon via an in-container forwarder.

    ``gh`` only speaks to a Unix socket (``http_unix_socket``), but the
    daemon is a TCP listener on the bridge. We record the bridge endpoint
    in ``/etc/bubble/gh/bridge`` and point gh's config at a user-owned
    socket path; the gh wrapper (installed by the gh tool script) lazily
    starts a ``socat`` unix→TCP forwarder to that endpoint on first use.
    No incus device, no host-side socket — so no forkproxy.
    """
    # owner/repo are repo identifiers (no shell metacharacters); the
    # endpoint is host:port. None of these are secret, so they may appear
    # in argv. Only $TOKEN is read from stdin.
    payload = _gh_proxy_profile_payload(owner, repo) + (
        "\nmkdir -p /etc/bubble/gh"
        f"\nprintf '%s' {shlex.quote(f'{host_ip}:{port}')} > /etc/bubble/gh/bridge"
        "\ncat > /etc/bubble/gh/config.yml <<GHCONF\n"
        'version: "1"\n'
        f"http_unix_socket: {_CONTAINER_GH_SOCKET_BRIDGE}\n"
        "GHCONF\n"
        "chown -R 1001:1001 /etc/bubble/gh\n"
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


def setup_auth_proxy_remote(
    remote_host,
    container: str,
    owner: str,
    repo: str,
    machine_readable: bool = False,
    gh_enabled: bool = False,
    config: dict | None = None,
    push_repos: list[str] | None = None,
) -> bool:
    """Set up auth proxy access for a container on a remote host.

    Tunnels the local auth proxy to the remote host via SSH reverse
    port forwarding, adds an Incus proxy device on the remote to
    expose the tunneled port into the container, and configures git.

    ``push_repos`` lists additional ``owner/repo`` forks the container may
    git fetch from and push to (REST/GraphQL stay scoped to owner/repo).

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
        push_repos=push_repos,
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
        if push_repos:
            detail(f"  git fetch/push also allowed to fork(s): {', '.join(push_repos)}")
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
    push_repos: list[str] | None = None,
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
            push_repos=push_repos,
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
            push_repos=push_repos,
        )

    return False
