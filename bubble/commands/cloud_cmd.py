"""The 'cloud' command group: provision, destroy, stop, start, status, ssh, default."""

import click

from ..config import load_config, save_config


def register_cloud_commands(main):
    """Register the 'cloud' command group on the main CLI group."""

    @main.group("cloud")
    def cloud_group():
        """Manage Hetzner Cloud server for remote bubbles."""

    @cloud_group.command("provision")
    @click.option(
        "--type", "server_type", type=str, default=None, help="Server type (e.g. cx43, ccx43, cx53)"
    )
    @click.option("--location", type=str, default=None, help="Datacenter location (default: fsn1)")
    @click.option(
        "--list",
        "list_types",
        is_flag=True,
        default=False,
        help="List available server types and exit",
    )
    def cloud_provision(server_type, location, list_types):
        """Provision a Hetzner Cloud server for bubble.

        Creates a server with Incus pre-installed. The server auto-shuts down
        after 15 minutes of idle (no SSH connections + low CPU) to reduce costs.
        It auto-starts again on next 'bubble --cloud <target>'.

        \b
        Common server types (default: cx43):
          cx43     8 shared vCPU, 16GB RAM (~EUR 0.02/hr)
          cx53    16 shared vCPU, 32GB RAM (~EUR 0.04/hr)
          ccx43   16 dedicated vCPU, 64GB RAM (~EUR 0.17/hr)  # needs limit increase

        Use --list to see all available server types with current pricing.
        """
        config = load_config()
        if list_types:
            from ..cloud_types import list_server_types

            list_server_types(config, location=location)
            return

        from ..cloud import provision_server

        if not server_type:
            click.echo("Use --list to see all available server types.")
        provision_server(config, server_type=server_type, location=location)

    @cloud_group.command("destroy")
    @click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
    def cloud_destroy(force):
        """Destroy the cloud server permanently."""
        from ..cloud import destroy_server

        destroy_server(force=force)

    @cloud_group.command("stop")
    def cloud_stop():
        """Power off the cloud server.

        Containers are preserved on disk and will be available after restart.
        Note: Hetzner bills servers until deleted. Use 'bubble cloud destroy' to stop billing.
        """
        from ..cloud import stop_server

        stop_server()

    @cloud_group.command("start")
    def cloud_start():
        """Power on the cloud server and wait for SSH."""
        from ..cloud import start_server

        start_server()

    @cloud_group.command("status")
    def cloud_status():
        """Show cloud server info and status."""
        from ..cloud import get_server_status

        status = get_server_status()
        if not status:
            click.echo("No cloud server provisioned.")
            click.echo("Set one up with: bubble cloud provision")
            return

        click.echo(f"  Server:   {status.get('server_name', '?')}")
        click.echo(f"  ID:       {status.get('server_id', '?')}")
        click.echo(f"  IP:       {status.get('ipv4', '?')}")
        click.echo(f"  Type:     {status.get('server_type', '?')}")
        click.echo(f"  Location: {status.get('location', '?')}")
        click.echo(f"  Status:   {status.get('status', 'unknown')}")
        if status.get("server_type_description"):
            click.echo(f"  Specs:    {status['server_type_description']}")

    @cloud_group.command("ssh")
    @click.argument("args", nargs=-1)
    def cloud_ssh_cmd(args):
        """SSH directly to the cloud server."""
        from ..cloud import cloud_ssh

        cloud_ssh(list(args) if args else None)

    @cloud_group.command("default")
    @click.argument("setting", required=False, type=click.Choice(["on", "off"]))
    def cloud_default(setting):
        """Set whether cloud is the default for all 'bubble open'.

        When on, all bubbles go to cloud unless --local is used.
        Shows current setting if no argument given.
        """
        config = load_config()
        if setting is None:
            current = config.get("cloud", {}).get("default", False)
            state = "on" if current else "off"
            click.echo(f"Cloud default: {state}")
            if current:
                click.echo("All 'bubble open' commands use cloud. Use --local to override.")
            else:
                click.echo("Use --cloud flag or: bubble cloud default on")
            return
        config.setdefault("cloud", {})["default"] = setting == "on"
        save_config(config)
        if setting == "on":
            click.echo("Cloud set as default. All 'bubble open' will use cloud.")
            click.echo("Override with: bubble open --local <target>")
        else:
            click.echo("Cloud default disabled. Use --cloud flag for cloud bubbles.")
