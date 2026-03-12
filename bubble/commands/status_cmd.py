"""The 'status' command: concise dashboard of current bubble state."""

import click

from ..config import load_config
from ..images.builder import is_builder_container
from ..lifecycle import load_registry
from ..setup import get_runtime


def _bubble_counts(config: dict) -> dict[str, int]:
    """Count containers by state. Returns {state: count} dict.

    Uses the runtime's list_containers (fast mode) which avoids expensive
    per-container queries. Falls back to registry-only counts if the
    runtime is unavailable.
    """

    counts: dict[str, int] = {}
    try:
        runtime = get_runtime(config, ensure_ready=False)
        for c in runtime.list_containers(fast=True):
            if is_builder_container(c.name):
                continue
            state = c.state
            counts[state] = counts.get(state, 0) + 1
    except Exception:
        # Runtime unavailable — count from registry only
        registry = load_registry()
        n = len(registry.get("bubbles", {}))
        if n:
            counts["registered"] = n
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    """Format state counts into a compact string like '3 running, 1 paused'."""
    if not counts:
        return "none"
    # Preferred display order
    order = ["running", "frozen", "stopped", "registered"]
    # Map internal state names to display names
    display = {"frozen": "paused"}
    parts = []
    for state in order:
        if state in counts:
            label = display.get(state, state)
            parts.append(f"{counts[state]} {label}")
    # Any states not in the preferred order
    for state, n in sorted(counts.items()):
        if state not in order:
            parts.append(f"{n} {state}")
    return ", ".join(parts) if parts else "none"


def _cloud_summary() -> str | None:
    """Get cloud status from local state file (no API call)."""
    from ..cloud import _load_state

    state = _load_state()
    if not state:
        return None
    server_type = state.get("server_type", "?")
    location = state.get("location", "?")
    name = state.get("server_name", "?")
    return f"{name} ({server_type} in {location})"


def _remote_summary(config: dict) -> str | None:
    """Get default remote host from config."""
    default = config.get("remote", {}).get("default_host", "")
    return default or None


def _tools_summary(config: dict) -> str:
    """Get enabled tool names."""
    from ..tools import resolve_tools

    tools = resolve_tools(config)
    return ", ".join(tools) if tools else "none"


def _automation_summary() -> str | None:
    """Get automation status summary."""
    from ..automation import is_automation_installed

    status = is_automation_installed()
    if not status:
        return None
    installed = [name for name, ok in status.items() if ok]
    if not installed:
        return "not installed"
    total = len(status)
    if len(installed) == total:
        return "installed"
    return f"{len(installed)}/{total} installed"


def _warnings(config: dict, counts: dict[str, int]) -> list[str]:
    """Collect warning messages for broken/disconnected subsystems."""
    warns = []

    # Check if cloud is configured as default but no server exists
    cloud_default = config.get("cloud", {}).get("default", False)
    if cloud_default:
        from ..cloud import _load_state

        if not _load_state():
            warns.append("Cloud is default but no server provisioned (run: bubble cloud provision)")

    return warns


def register_status_command(main):
    """Register the 'status' command on the main CLI group."""

    @main.command("status")
    @click.option("-v", "--verbose", is_flag=True, help="Show per-subsystem details")
    def status_cmd(verbose):
        """Show a concise summary of bubble state."""
        config = load_config()

        counts = _bubble_counts(config)
        remote = _remote_summary(config)
        cloud = _cloud_summary()
        tools = _tools_summary(config)

        # Warnings first
        warns = _warnings(config, counts)
        for w in warns:
            click.echo(f"Warning: {w}")
        if warns:
            click.echo()

        # Compute label width for alignment
        labels = ["Bubbles"]
        if remote:
            labels.append("Remote")
        if cloud:
            labels.append("Cloud")
        labels.append("Tools")
        width = max(len(label) for label in labels) + 1  # +1 for the colon

        click.echo(f"{'Bubbles:':<{width}}  {_format_counts(counts)}")
        if remote:
            click.echo(f"{'Remote:':<{width}}  {remote}")
        if cloud:
            click.echo(f"{'Cloud:':<{width}}  {cloud}")
        click.echo(f"{'Tools:':<{width}}  {tools}")

        if verbose:
            click.echo()

            # Per-bubble details
            registry = load_registry()
            bubbles = registry.get("bubbles", {})
            if bubbles:
                click.echo("Bubbles:")
                for name, info in bubbles.items():
                    repo = info.get("org_repo", "")
                    host = info.get("remote_host", "")
                    parts = [name]
                    if repo:
                        parts.append(repo)
                    if host:
                        parts.append(f"on {host}")
                    elif info.get("native"):
                        parts.append("native")
                    click.echo(f"  {' — '.join(parts)}")

            # Remote bubbles
            remote_bubbles = [(n, i) for n, i in bubbles.items() if i.get("remote_host")]
            if remote_bubbles:
                hosts = {i["remote_host"] for _, i in remote_bubbles}
                click.echo(f"\nRemote hosts: {', '.join(sorted(hosts))}")

            # Automation
            auto = _automation_summary()
            if auto:
                click.echo(f"Automation: {auto}")
