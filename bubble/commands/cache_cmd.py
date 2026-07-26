"""Commands for Bubble's host-global GET-only artifact cache."""

import click


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def register_cache_commands(main):
    @main.group("cache")
    def cache_group():
        """Manage the host-global download-only artifact cache."""

    @cache_group.command("status")
    def cache_status():
        """Show daemon state and cached object usage."""
        from ..artifact_cache import (
            ARTIFACT_CACHE_TOKENS,
            cache_stats,
            load_host_artifact_config,
            parse_size,
            read_live_endpoint,
        )
        from ..automation import is_artifact_cache_installed
        from ..token_store import TokenStore

        count, size = cache_stats()
        configured = load_host_artifact_config().get("max_size", "50GiB")
        try:
            limit = parse_size(configured)
            limit_label = _human_bytes(limit)
        except ValueError:
            limit_label = f"invalid ({configured})"
        endpoint = read_live_endpoint()
        click.echo(
            f"Artifact cache:  {'running' if endpoint else 'stopped'}"
            f" ({'installed' if is_artifact_cache_installed() else 'not installed'})"
        )
        if endpoint:
            tcp = endpoint["tcp"]
            click.echo(f"Endpoint:        {tcp['host']}:{tcp['port']}")
        click.echo(f"Objects:         {count}")
        click.echo(f"Disk usage:      {_human_bytes(size)} / {limit_label}")
        grants = TokenStore(ARTIFACT_CACHE_TOKENS).values()
        holders = {str(grant.get("container")) for grant in grants if grant.get("container")}
        click.echo(f"Capability holders: {len(holders)}")

    @cache_group.command("start")
    def cache_start():
        """Install and start the artifact cache daemon."""
        from ..artifact_cache import wait_for_endpoint
        from ..automation import install_artifact_cache_daemon

        try:
            result = install_artifact_cache_daemon()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if not result:
            raise click.ClickException("artifact cache daemon is unsupported on this platform")
        if wait_for_endpoint(compatible=True) is None:
            raise click.ClickException(
                "artifact cache did not publish a live endpoint; see "
                "~/.bubble/artifact-cache.log and /tmp/bubble-artifact-cache.log"
            )
        click.echo(f"Artifact cache daemon installed: {result}")

    @cache_group.command("stop")
    def cache_stop():
        """Stop and uninstall the artifact cache daemon (objects remain)."""
        from ..artifact_cache import ARTIFACT_CACHE_TOKENS
        from ..automation import remove_artifact_cache_daemon
        from ..token_store import TokenStore

        holders = {
            str(grant.get("container"))
            for grant in TokenStore(ARTIFACT_CACHE_TOKENS).values()
            if grant.get("container")
        }
        if holders:
            click.echo(
                f"Warning: cache downloads in {len(holders)} bubble(s) will fail until "
                "`bubble cache start` is run.",
                err=True,
            )
        result = remove_artifact_cache_daemon()
        click.echo(f"Artifact cache daemon removed: {result}" if result else "Daemon not found.")

    @cache_group.command("prune")
    @click.option(
        "--max-size",
        default=None,
        metavar="SIZE",
        help="Target size such as 20GiB (default: configured artifact_cache.max_size).",
    )
    @click.option("--execute", is_flag=True, help="Delete selected objects (default is a dry run).")
    def cache_prune(max_size, execute):
        """Select least-recently-used objects until the cache fits its limit."""
        from ..artifact_cache import load_host_artifact_config, parse_size, prune_cache

        value = max_size or load_host_artifact_config().get("max_size", "50GiB")
        try:
            limit = parse_size(value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        count, size = prune_cache(limit, execute=execute)
        verb = "Deleted" if execute else "Would delete"
        click.echo(f"{verb} {count} object(s), {_human_bytes(size)}.")
        if not execute and count:
            click.echo("Run again with --execute to apply.")

    @cache_group.command("clear")
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    def cache_clear(yes):
        """Delete every cached response body (the daemon remains installed)."""
        from ..artifact_cache import clear_cache

        if not yes:
            click.confirm("Delete all host-global Bubble artifact cache objects?", abort=True)
        count, size = clear_cache()
        click.echo(f"Deleted {count} object(s), {_human_bytes(size)}.")

    @cache_group.command("daemon", hidden=True)
    @click.option("--port", type=int, default=0, help="Port to listen on (0 = configured default).")
    @click.option("--max-size", default=None, metavar="SIZE")
    def cache_daemon(port, max_size):
        """Run the artifact cache daemon (used by launchd/systemd)."""
        from ..artifact_cache import parse_size, run_daemon

        try:
            limit = parse_size(max_size) if max_size is not None else None
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        run_daemon(port=port, max_bytes=limit)
