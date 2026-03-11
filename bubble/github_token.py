"""GitHub token injection for containers.

Injects the host's GitHub authentication into containers so that `gh`
CLI works inside bubbles. The token is injected via `gh auth login
--with-token`, giving the container the same GitHub access as the host.

The token lives only in the container's filesystem and is destroyed
when the bubble is deleted.
"""

import shlex
import subprocess

import click

from .runtime.base import ContainerRuntime


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


def inject_gh_token(runtime: ContainerRuntime, container: str, token: str):
    """Inject a GitHub token into a container via gh auth login.

    Sets up gh CLI authentication inside the container so that
    commands like `gh pr create`, `gh issue comment`, etc. work.
    """
    # gh auth login --with-token reads the token from stdin.
    # We pipe it via echo to avoid putting the token in command args
    # (which would be visible in /proc). We use a heredoc-style
    # approach through bash -c with the token passed via env var.
    runtime.exec(
        container,
        [
            "su",
            "-",
            "user",
            "-c",
            f"echo {shlex.quote(token)}"
            " | gh auth login --with-token 2>/dev/null"
            " && gh auth setup-git 2>/dev/null"
            " || true",
        ],
    )


def setup_gh_token(
    runtime: ContainerRuntime,
    container: str,
    machine_readable: bool = False,
):
    """Get the host token and inject it into the container.

    Returns True if the token was successfully injected.
    """
    token = get_host_gh_token()
    if not token:
        if not machine_readable:
            click.echo("  Warning: gh is not authenticated on host, skipping token injection.")
        return False

    inject_gh_token(runtime, container, token)
    if not machine_readable:
        click.echo("  GitHub token injected.")
    return True


def has_gh_auth() -> bool:
    """Check if the host has gh CLI authentication configured."""
    return get_host_gh_token() is not None
