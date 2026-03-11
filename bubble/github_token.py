"""GitHub token injection for containers.

Injects the host's GitHub authentication into containers so that `gh`
CLI works inside bubbles. The token is injected via `gh auth login
--with-token`, giving the container the same GitHub access as the host.

The token lives only in the container's filesystem and is destroyed
when the bubble is deleted.

Security: the token is written to a temp file and pushed into the
container via `incus file push`, then consumed and deleted. It never
appears in process arguments or error messages.
"""

import subprocess
import tempfile
from pathlib import Path

import click

from .runtime.base import ContainerRuntime

# Path inside the container where the token is temporarily stored.
_CONTAINER_TOKEN_PATH = "/tmp/.gh-token"


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
    # The token file is the only place the secret exists — it never appears
    # in argv or environment variables.
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
    machine_readable: bool = False,
    remote_host=None,
) -> bool:
    """Get the host token and inject it into the container.

    For local containers, uses push_file + exec. For remote containers,
    pipes the token via SSH stdin.

    Returns True if the token was successfully injected.
    """
    token = get_host_gh_token()
    if not token:
        if not machine_readable:
            click.echo("  Warning: gh is not authenticated on host, skipping token injection.")
        return False

    if remote_host:
        success = inject_gh_token_remote(remote_host, container, token)
    else:
        success = inject_gh_token(runtime, container, token)
    if not machine_readable:
        if success:
            click.echo("  GitHub token injected.")
        else:
            click.echo("  Warning: GitHub token injection failed.")
    return success
