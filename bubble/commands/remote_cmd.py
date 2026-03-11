"""The 'remote' command group: set-default, clear-default, status."""

import click

from ..config import load_config, save_config
from ..lifecycle import load_registry


def register_remote_commands(main):
    """Register the 'remote' command group on the main CLI group."""

    @main.group("remote")
    def remote_group():
        """Manage remote SSH host settings."""

    @remote_group.command("set-default")
    @click.argument("host")
    def remote_set_default(host):
        """Set the default remote SSH host for new bubbles.

        HOST can be: hostname, user@hostname, or user@hostname:port
        """
        from ..remote import RemoteHost

        # Validate the spec parses
        parsed = RemoteHost.parse(host)
        config = load_config()
        if "remote" not in config:
            config["remote"] = {}
        config["remote"]["default_host"] = parsed.spec_string()
        save_config(config)
        click.echo(f"Default remote host set to: {parsed.spec_string()}")

    @remote_group.command("clear-default")
    def remote_clear_default():
        """Clear the default remote SSH host."""
        config = load_config()
        if "remote" in config:
            config["remote"]["default_host"] = ""
            save_config(config)
        click.echo("Default remote host cleared.")

    @remote_group.command("status")
    def remote_status():
        """Show current remote host configuration."""
        config = load_config()
        default = config.get("remote", {}).get("default_host", "")
        if default:
            click.echo(f"Default remote host: {default}")
        else:
            click.echo("No default remote host configured.")

        # Show remote bubbles from registry
        registry = load_registry()
        remote_bubbles = [
            (name, info)
            for name, info in registry.get("bubbles", {}).items()
            if info.get("remote_host")
        ]
        if remote_bubbles:
            click.echo(f"\nRemote bubbles ({len(remote_bubbles)}):")
            for name, info in remote_bubbles:
                click.echo(f"  {name:<30} {info['remote_host']}")
