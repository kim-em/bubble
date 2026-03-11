"""Small infrastructure command groups: git, network, automation."""

import click

from ..config import load_config
from ..container_helpers import ensure_running
from ..git_store import update_all_repos
from ..setup import get_runtime


def register_infrastructure_commands(main):
    """Register git, network, and automation command groups on the main CLI group."""

    # --- git ---

    @main.group("git")
    def git_group():
        """Manage shared git object store."""

    @git_group.command("update")
    def git_update():
        """Update all shared bare repos."""
        update_all_repos()
        click.echo("Git store updated.")

    # --- network ---

    @main.group("network")
    def network_group():
        """Manage network allowlisting."""

    @network_group.command("apply")
    @click.argument("name")
    def network_apply(name):
        """Apply network allowlist to a bubble."""
        from ..container_helpers import apply_network

        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        ensure_running(runtime, name)

        apply_network(runtime, name, config)

    @network_group.command("remove")
    @click.argument("name")
    def network_remove(name):
        """Remove network restrictions from a bubble."""
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        ensure_running(runtime, name)

        from ..network import remove_allowlist

        remove_allowlist(runtime, name)
        click.echo(f"Network restrictions removed from '{name}'.")

    # --- automation ---

    @main.group("automation")
    def automation_group():
        """Manage automated tasks (git update, image refresh)."""

    @automation_group.command("install")
    def automation_install():
        """Install automation jobs (launchd on macOS, systemd on Linux)."""
        from ..automation import install_automation

        installed = install_automation()
        if installed:
            for item in installed:
                click.echo(f"  Installed: {item}")
            click.echo("Automation installed.")
        else:
            click.echo("No automation installed (unsupported platform?).", err=True)

    @automation_group.command("remove")
    def automation_remove():
        """Remove all automation jobs."""
        from ..automation import remove_automation

        removed = remove_automation()
        if removed:
            for item in removed:
                click.echo(f"  Removed: {item}")
            click.echo("Automation removed.")
        else:
            click.echo("No automation jobs found to remove.")

    @automation_group.command("status")
    def automation_status():
        """Show automation status."""
        from ..automation import is_automation_installed

        status = is_automation_installed()
        if not status:
            click.echo("Automation not supported on this platform.")
            return
        for job, installed in status.items():
            state = "installed" if installed else "not installed"
            click.echo(f"  {job}: {state}")
