"""Top-level `bubble security` command for reviewing and managing security posture."""

import sys

import click

from ..config import load_config, save_config
from ..security import (
    SETTINGS as SECURITY_SETTINGS,
)
from ..security import (
    apply_preset_default,
    apply_preset_lockdown,
    apply_preset_permissive,
    display_setting_name,
    normalize_setting_name,
    print_security_posture,
    valid_values_for,
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
                click.echo(f"  security.{display_setting_name(name)} = on")
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
                click.echo(f"  security.{display_setting_name(name)} = auto")
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
                click.echo(f"  security.{display_setting_name(name)} = off")
            click.echo(f"Set {len(changed)} setting(s) to off. Maximum isolation.")
        else:
            click.echo("All settings are already off.")

    @security_group.command("set")
    @click.argument("key")
    @click.argument("value")
    def security_set(key, value):
        """Set a security setting: bubble security set <name> <value>.

        Setting names use hyphens (e.g. github-auth, claude-credentials).
        Underscores are also accepted as permanent aliases.

        Most settings accept: auto, on, off.
        github-api also accepts: read-write (enables mutations).
        """
        # Accept both "security.X" and bare "X", normalize hyphens to underscores
        name = normalize_setting_name(key.removeprefix("security."))
        if name not in SECURITY_SETTINGS:
            available = ", ".join(sorted(display_setting_name(k) for k in SECURITY_SETTINGS))
            click.echo(f"Unknown security setting: {key}. Available: {available}", err=True)
            sys.exit(1)

        allowed = valid_values_for(name)
        if value not in allowed:
            click.echo(
                f"Invalid value {value!r} for {display_setting_name(name)}."
                f" Choose from: {', '.join(allowed)}",
                err=True,
            )
            sys.exit(1)

        config = load_config()
        if "security" not in config:
            config["security"] = {}
        config["security"][name] = value

        # Keep relay backwards compat in sync
        if name == "relay":
            config.setdefault("relay", {})["enabled"] = value == "on"

        save_config(config)
        click.echo(f"Set security.{display_setting_name(name)} = {value}")
