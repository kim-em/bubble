"""Top-level `bubble security` command for reviewing and managing security posture."""

import sys

import click

from ..config import load_config, save_config
from ..security import (
    SETTINGS as SECURITY_SETTINGS,
)
from ..security import (
    VALID_VALUES as SECURITY_VALID_VALUES,
)
from ..security import (
    apply_preset_default,
    apply_preset_lockdown,
    apply_preset_permissive,
    print_security_posture,
)


def register_security_commands(main):
    """Register the `bubble security` command group on the main CLI group."""

    @main.group("security", invoke_without_command=True)
    @click.pass_context
    def security_group(ctx):
        """Review and manage bubble's security posture."""
        if ctx.invoked_subcommand is None:
            config = load_config()
            print_security_posture(config)

    @security_group.command("permissive")
    def security_permissive():
        """Enable all conveniences (set everything to on)."""
        config = load_config()
        changed = apply_preset_permissive(config)
        if changed:
            save_config(config)
            for name in changed:
                click.echo(f"  security.{name} = on")
            click.echo(f"Set {len(changed)} setting(s) to on. All conveniences enabled.")
        else:
            click.echo("All settings are already on.")

    @security_group.command("default")
    def security_default():
        """Restore all settings to auto (factory defaults)."""
        config = load_config()
        changed = apply_preset_default(config)
        if changed:
            save_config(config)
            for name in changed:
                click.echo(f"  security.{name} = auto")
            click.echo(f"Reset {len(changed)} setting(s) to auto.")
        else:
            click.echo("All settings are already on auto.")

    @security_group.command("lockdown")
    def security_lockdown():
        """Disable everything risky (set everything to off)."""
        config = load_config()
        changed = apply_preset_lockdown(config)
        if changed:
            save_config(config)
            for name in changed:
                click.echo(f"  security.{name} = off")
            click.echo(f"Set {len(changed)} setting(s) to off. Maximum isolation.")
        else:
            click.echo("All settings are already off.")

    @security_group.command("set")
    @click.argument("key")
    @click.argument("value", type=click.Choice(SECURITY_VALID_VALUES))
    def security_set(key, value):
        """Set a security setting: bubble security set <name> <value>."""
        # Accept both "security.X" and bare "X"
        name = key.removeprefix("security.")
        if name not in SECURITY_SETTINGS:
            available = ", ".join(sorted(SECURITY_SETTINGS.keys()))
            click.echo(f"Unknown security setting: {name}. Available: {available}", err=True)
            sys.exit(1)

        config = load_config()
        if "security" not in config:
            config["security"] = {}
        config["security"][name] = value

        # Keep relay backwards compat in sync
        if name == "relay":
            config.setdefault("relay", {})["enabled"] = value == "on"
        # Clear legacy github token so it doesn't override the new setting
        if name == "github_auth" and "github" in config and "token" in config["github"]:
            del config["github"]["token"]

        save_config(config)
        click.echo(f"Set security.{name} = {value}")
