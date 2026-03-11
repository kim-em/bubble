"""The 'relay' command group: enable, disable, status, daemon."""

import sys

import click

from ..config import load_config, save_config
from ..security import get_setting, is_enabled, is_locked_off


def register_relay_commands(main):
    """Register the 'relay' command group on the main CLI group."""

    @main.group("relay")
    def relay_group():
        """Manage the bubble-in-bubble relay."""

    @relay_group.command("enable")
    def relay_enable():
        """Enable bubble-in-bubble relay.

        This allows containers to request creation of new bubbles on the host.
        Only repos already cloned in ~/.bubble/git/ can be opened via relay.
        All relay requests are rate-limited and logged.
        """
        click.echo("Enabling bubble-in-bubble relay.")
        click.echo()
        click.echo("This opens a controlled channel from containers to the host.")
        click.echo("Mitigations: known repos only, rate limiting, request logging.")
        click.echo()

        config = load_config()
        # Check if relay is explicitly locked off
        if is_locked_off(config, "relay"):
            click.echo(
                "Error: relay is locked off (security.relay=off). "
                "Re-enable: bubble config set security.relay on",
                err=True,
            )
            sys.exit(1)

        config.setdefault("security", {})["relay"] = "on"
        save_config(config)

        # Install and start the relay daemon
        from ..automation import install_relay_daemon

        try:
            result = install_relay_daemon()
            if result:
                click.echo(f"  Installed: {result}")
        except Exception as e:
            click.echo(f"  Warning: could not install daemon: {e}")
            click.echo("  You can start it manually with: bubble relay daemon")

        click.echo()
        click.echo("Relay enabled. New bubbles will include the relay socket.")
        click.echo("Existing bubbles need to be recreated to get relay access.")

    @relay_group.command("disable")
    def relay_disable():
        """Disable bubble-in-bubble relay."""
        config = load_config()
        # Reset security.relay to auto (not off) so relay enable works as a toggle.
        # Use 'bubble security set relay off' to permanently lock it off.
        config.setdefault("security", {}).pop("relay", None)
        save_config(config)

        from ..automation import remove_relay_daemon

        try:
            result = remove_relay_daemon()
            if result:
                click.echo(f"  Removed: {result}")
        except Exception:
            pass

        # Remove socket/port file
        from ..relay import RELAY_PORT_FILE, RELAY_SOCK

        RELAY_SOCK.unlink(missing_ok=True)
        RELAY_PORT_FILE.unlink(missing_ok=True)

        click.echo("Relay disabled.")

    @relay_group.command("status")
    def relay_status():
        """Show relay status."""
        import platform

        config = load_config()
        enabled = is_enabled(config, "relay")
        click.echo(f"  Relay: {'enabled' if enabled else 'disabled'}")
        click.echo(f"  Security setting: {get_setting(config, 'relay')}")

        from ..relay import RELAY_PORT_FILE, RELAY_SOCK

        if platform.system() == "Darwin":
            if RELAY_PORT_FILE.exists():
                port = RELAY_PORT_FILE.read_text().strip()
                click.echo(f"  Listening: TCP 127.0.0.1:{port}")
            else:
                click.echo("  Listening: not running")
        else:
            click.echo(f"  Socket: {'exists' if RELAY_SOCK.exists() else 'not found'}")

        from ..relay import RELAY_LOG

        if RELAY_LOG.exists():
            # Show last 5 log entries
            lines = RELAY_LOG.read_text().strip().splitlines()
            if lines:
                click.echo(f"  Log ({len(lines)} entries, last 5):")
                for line in lines[-5:]:
                    click.echo(f"    {line}")
            else:
                click.echo("  Log: empty")
        else:
            click.echo("  Log: no requests yet")

    @relay_group.command("daemon")
    def relay_daemon_cmd():
        """Run the relay daemon (used by launchd/systemd)."""
        from ..relay import run_daemon

        run_daemon()
