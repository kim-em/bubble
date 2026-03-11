"""Settings command groups: skill, claude, tools, gh, config."""

import sys

import click

from ..config import load_config, save_config
from ..security import SETTINGS as SECURITY_SETTINGS
from ..security import VALID_VALUES as SECURITY_VALID_VALUES
from ..security import get_setting


def register_settings_commands(main):
    """Register skill, claude, tools, gh, and config command groups on the main CLI group."""

    # --- skill ---

    @main.group("skill")
    def skill_group():
        """Manage the Claude Code bubble skill."""

    @skill_group.command("install")
    def skill_install():
        """Install the bubble skill into ~/.claude/skills/."""
        from ..skill import (
            claude_code_detected,
            diff_skill,
            install_skill,
            is_installed,
            is_up_to_date,
        )

        if not claude_code_detected():
            click.echo("~/.claude/ not found — Claude Code not detected. Skipping.")
            return

        if is_installed() and is_up_to_date():
            click.echo("Bubble skill is already installed and up to date.")
            return

        if is_installed():
            d = diff_skill()
            if d:
                click.echo("Updating bubble skill:")
                click.echo(d)

        msg = install_skill()
        click.echo(msg)

    @skill_group.command("uninstall")
    def skill_uninstall():
        """Remove the bubble skill from ~/.claude/skills/."""
        from ..skill import uninstall_skill

        msg = uninstall_skill()
        click.echo(msg)

    @skill_group.command("status")
    def skill_status():
        """Check if the bubble skill is installed and up to date."""
        from ..skill import claude_code_detected, is_installed, is_up_to_date

        if not claude_code_detected():
            click.echo("Claude Code not detected (~/.claude/ not found).")
            return

        if not is_installed():
            click.echo("Bubble skill is not installed.")
            click.echo("  Install with: bubble skill install")
            return

        if is_up_to_date():
            click.echo("Bubble skill is installed and up to date.")
        else:
            click.echo("Bubble skill is installed but outdated.")
            click.echo("  Update with: bubble skill install")

    # --- claude ---

    @main.group("claude")
    def claude_group():
        """Manage Claude Code settings."""

    @claude_group.command("credentials")
    @click.argument("setting", required=False, type=click.Choice(["on", "off"]))
    def claude_credentials_cmd(setting):
        """Set whether Claude credentials are mounted into bubbles.

        When on, ~/.claude credentials (.credentials.json) are mounted
        read-only into containers by default. Override per-bubble with
        --no-claude-credentials.

        Shows current setting if no argument given.
        """
        config = load_config()
        if setting is None:
            current = config.get("claude", {}).get("credentials", False)
            state = "on" if current else "off"
            click.echo(f"Claude credentials: {state}")
            if current:
                click.echo("Credentials are mounted into bubbles by default.")
                click.echo("Override with: bubble open --no-claude-credentials <target>")
            else:
                click.echo("Use --claude-credentials flag or: bubble claude credentials on")
            return
        config.setdefault("claude", {})["credentials"] = setting == "on"
        save_config(config)
        if setting == "on":
            click.echo("Claude credentials enabled. Mounted into all new bubbles by default.")
            click.echo("Override with: bubble open --no-claude-credentials <target>")
        else:
            click.echo("Claude credentials disabled.")

    @claude_group.command("status")
    def claude_status_cmd():
        """Show current Claude Code settings."""
        config = load_config()
        creds = config.get("claude", {}).get("credentials", False)
        click.echo(f"  credentials: {'on' if creds else 'off'}")

    # --- tools ---

    @main.group("tools")
    def tools_group():
        """Manage tools installed in container images."""

    @tools_group.command("list")
    def tools_list():
        """List available tools and their current settings."""
        from ..tools import available_tools

        config = load_config()
        tools_config = config.get("tools", {})

        click.echo(f"{'TOOL':<20} {'SETTING':<10}")
        click.echo("-" * 30)
        for name in available_tools():
            setting = tools_config.get(name, "auto")
            click.echo(f"{name:<20} {setting:<10}")

    @tools_group.command("set")
    @click.argument("tool_name")
    @click.argument("value", type=click.Choice(["yes", "no", "auto"]))
    def tools_set(tool_name, value):
        """Set a tool to yes, no, or auto."""
        from ..tools import TOOLS

        if tool_name not in TOOLS:
            available = ", ".join(sorted(TOOLS.keys()))
            click.echo(f"Unknown tool: {tool_name}. Available: {available}", err=True)
            sys.exit(1)

        config = load_config()
        if "tools" not in config:
            config["tools"] = {}
        config["tools"][tool_name] = value
        save_config(config)
        click.echo(f"Set {tool_name} = {value}")
        click.echo("Run 'bubble images build base' to apply changes.")

    @tools_group.command("status")
    def tools_status():
        """Show which tools would be installed (resolved state)."""
        from ..tools import TOOLS, resolve_tools, tools_hash

        config = load_config()
        enabled = resolve_tools(config)
        tools_config = config.get("tools", {})

        click.echo(f"{'TOOL':<20} {'SETTING':<10} {'RESOLVED':<10}")
        click.echo("-" * 40)
        for name in sorted(TOOLS.keys()):
            setting = tools_config.get(name, "auto")
            resolved = "install" if name in enabled else "skip"
            click.echo(f"{name:<20} {setting:<10} {resolved:<10}")

        if enabled:
            click.echo(f"\nTools hash: {tools_hash(enabled)}")
        else:
            click.echo("\nNo tools will be installed.")

    @tools_group.command("update")
    def tools_update():
        """Fetch latest upstream versions and update pinned versions.

        Checks nodejs.org, npmjs.org, and cli.github.com for the latest
        versions and checksums, then updates the local pins. This is a
        maintainer workflow — the updated pins should be committed and
        released so users get the new versions via package upgrade.
        """
        from ..tools import fetch_latest_pins, load_pins, save_pins

        click.echo("Fetching latest versions from upstream...")
        try:
            new_pins = fetch_latest_pins()
        except Exception as e:
            click.echo(f"Error fetching upstream versions: {e}", err=True)
            sys.exit(1)

        current = load_pins()
        changes = []
        for key in sorted(new_pins):
            old = current.get(key, "(not set)")
            new = new_pins[key]
            if old != new:
                changes.append((key, old, new))

        if not changes:
            click.echo("All pins are up to date.")
            return

        click.echo(f"\n{'PIN':<25} {'CURRENT':<20} {'LATEST':<20}")
        click.echo("-" * 65)
        for key, old, new in changes:
            # Truncate checksums for display
            old_display = old[:16] + "..." if len(old) > 20 else old
            new_display = new[:16] + "..." if len(new) > 20 else new
            click.echo(f"{key:<25} {old_display:<20} {new_display:<20}")

        click.echo()
        save_pins(new_pins)
        click.echo("Pins updated. Run 'bubble images build base' to apply changes.")

    # --- gh ---

    @main.group("gh")
    def gh_group():
        """Manage GitHub integration settings."""

    @gh_group.command("status")
    def gh_status():
        """Show GitHub integration status."""
        from ..automation import is_auth_proxy_installed
        from ..github_token import has_gh_auth
        from ..security import is_enabled as sec_is_enabled

        config = load_config()
        github_auth = get_setting(config, "github_auth")
        enabled = sec_is_enabled(config, "github_auth")
        host_auth = has_gh_auth()
        proxy_installed = is_auth_proxy_installed()

        click.echo(f"GitHub auth:      {github_auth} (effectively {'on' if enabled else 'off'})")
        click.echo(f"Host gh auth:     {'authenticated' if host_auth else 'not authenticated'}")
        click.echo(f"Auth proxy:       {'installed' if proxy_installed else 'not installed'}")
        if not host_auth:
            click.echo("\nRun 'gh auth login' to authenticate on the host first.")
        elif not enabled:
            click.echo(
                "\nRun 'bubble security set github_auth on' to enable GitHub auth in bubbles."
            )

    @gh_group.group("proxy")
    def gh_proxy_group():
        """Manage the GitHub auth proxy daemon."""

    @gh_proxy_group.command("daemon")
    @click.option("--port", type=int, default=0, help="Port to listen on (0 = auto)")
    def gh_proxy_daemon(port):
        """Run the auth proxy daemon (used by launchd/systemd)."""
        from ..auth_proxy import run_daemon

        run_daemon(port=port)

    @gh_proxy_group.command("start")
    def gh_proxy_start():
        """Install and start the auth proxy daemon."""
        from ..automation import install_auth_proxy_daemon

        result = install_auth_proxy_daemon()
        if result:
            click.echo(f"Auth proxy daemon installed: {result}")
        else:
            click.echo("Failed to install auth proxy daemon (unsupported platform?).", err=True)

    @gh_proxy_group.command("stop")
    def gh_proxy_stop():
        """Stop and remove the auth proxy daemon."""
        from ..automation import remove_auth_proxy_daemon

        result = remove_auth_proxy_daemon()
        if result:
            click.echo(f"Auth proxy daemon removed: {result}")
        else:
            click.echo("Auth proxy daemon not found.")

    # --- config ---

    @main.group("config")
    def config_group():
        """View and manage bubble configuration."""

    @config_group.command("security")
    def config_security():
        """Show current security posture (use `bubble security` instead)."""
        click.echo("Hint: use `bubble security` for the full security dashboard.\n")
        from ..security import print_security_posture

        config = load_config()
        print_security_posture(config)

    @config_group.command("set")
    @click.argument("key")
    @click.argument("value", type=click.Choice(SECURITY_VALID_VALUES))
    def config_set(key, value):
        """Set a security setting: bubble config set security.<name> <value>."""
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

        save_config(config)
        click.echo(f"Set security.{name} = {value}")

    @config_group.command("lockdown")
    def config_lockdown():
        """Disable off-by-default features (use `bubble security lockdown` instead)."""
        click.echo("Hint: use `bubble security lockdown` for full lockdown.\n")
        config = load_config()
        if "security" not in config:
            config["security"] = {}

        changed = []
        for name, defn in SECURITY_SETTINGS.items():
            if get_setting(config, name) == "auto" and defn.auto_default == "off":
                config["security"][name] = "off"
                changed.append(name)

        if changed:
            save_config(config)
            for name in changed:
                click.echo(f"  security.{name} = off")
            click.echo(f"Locked down {len(changed)} setting(s).")
        else:
            click.echo("No auto-defaulting-to-off settings to lock down.")

    @config_group.command("accept-risks")
    def config_accept_risks():
        """Accept on-by-default risks (use `bubble security permissive` instead)."""
        click.echo("Hint: use `bubble security permissive` to enable all conveniences.\n")
        config = load_config()
        if "security" not in config:
            config["security"] = {}

        changed = []
        for name, defn in SECURITY_SETTINGS.items():
            if get_setting(config, name) == "auto" and defn.auto_default == "on":
                config["security"][name] = "on"
                changed.append(name)

        if changed:
            save_config(config)
            for name in changed:
                click.echo(f"  security.{name} = on")
            click.echo(f"Accepted {len(changed)} risk(s). On-by-default warnings silenced.")
        else:
            click.echo("No auto-defaulting-to-on settings to accept.")

    @config_group.command("symlink-claude-projects")
    def config_symlink_claude_projects():
        """Replace ~/.bubble/claude-projects/ with a symlink to ~/.claude/projects/.

        If ~/.claude/projects/ is inside a git repo, this command merges any
        existing session data from ~/.bubble/claude-projects/ into
        ~/.claude/projects/ and replaces the directory with a symlink.

        This lets bubble session state live inside the git-tracked directory
        and get synced across machines automatically.
        """
        from ..config import do_symlink_claude_projects

        if not do_symlink_claude_projects():
            raise SystemExit(1)
