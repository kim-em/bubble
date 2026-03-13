"""Settings command groups: skill, claude, tools, gh, config."""

import copy
import sys

import click

from ..config import DEFAULT_CONFIG, _deep_merge, load_config, load_raw_config, save_config
from ..security import SETTINGS as SECURITY_SETTINGS
from ..security import display_setting_name, get_setting, normalize_setting_name, valid_values_for


def _origin(section: str, key: str | None, config: dict, defaults: dict) -> str:
    """Return an origin annotation for a config value.

    Compares the effective value against the default. Values matching
    the default are annotated '(default)'; others are '(set in config)'.
    This handles the fact that load_config() writes defaults to disk,
    so we can't rely on file presence alone.
    """
    if key is None:
        # Top-level scalar (e.g. editor)
        default_val = defaults.get(section)
        effective_val = config.get(section)
        if effective_val == default_val:
            return "(default)"
        return "(set in config)"
    default_section = defaults.get(section, {})
    effective_section = config.get(section, {})
    if isinstance(default_section, dict) and isinstance(effective_section, dict):
        default_val = default_section.get(key)
        effective_val = effective_section.get(key)
        if effective_val == default_val:
            return "(default)"
        if key not in default_section:
            # Key exists in config but not in defaults
            return "(set in config)"
        return "(set in config)"
    return "(set in config)"


def _format_value(value) -> str:
    """Format a config value for display."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ", ".join(_format_value(v) for v in value)
        return f"[{items}]"
    if isinstance(value, dict):
        items = ", ".join(f"{k} = {_format_value(v)}" for k, v in value.items())
        return f"{{{items}}}"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def register_settings_commands(main):
    """Register skill, claude, tools, gh, and config command groups on the main CLI group."""

    # --- skill ---

    @main.group("skill", hidden=True)
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

    @main.group("claude", hidden=True)
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
            current = config.get("claude", {}).get("credentials", True)
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
        creds = config.get("claude", {}).get("credentials", True)
        click.echo(f"  credentials: {'on' if creds else 'off'}")

    # --- codex ---

    @main.group("codex", hidden=True)
    def codex_group():
        """Manage Codex/OpenAI settings."""

    @codex_group.command("credentials")
    @click.argument("setting", required=False, type=click.Choice(["on", "off"]))
    def codex_credentials_cmd(setting):
        """Set whether Codex credentials are mounted into bubbles.

        When on, ~/.codex credentials (auth.json) are mounted
        read-only into containers by default. Override per-bubble with
        --no-codex-credentials.

        Shows current setting if no argument given.
        """
        config = load_config()
        if setting is None:
            current = config.get("codex", {}).get("credentials", True)
            state = "on" if current else "off"
            click.echo(f"Codex credentials: {state}")
            if current:
                click.echo("Credentials are mounted into bubbles by default.")
                click.echo("Override with: bubble open --no-codex-credentials <target>")
            else:
                click.echo("Use --codex-credentials flag or: bubble codex credentials on")
            return
        config.setdefault("codex", {})["credentials"] = setting == "on"
        save_config(config)
        if setting == "on":
            click.echo("Codex credentials enabled. Mounted into all new bubbles by default.")
            click.echo("Override with: bubble open --no-codex-credentials <target>")
        else:
            click.echo("Codex credentials disabled.")

    @codex_group.command("status")
    def codex_status_cmd():
        """Show current Codex settings."""
        config = load_config()
        creds = config.get("codex", {}).get("credentials", True)
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

        Checks nodejs.org and npmjs.org for the latest versions and
        checksums, then updates the local pins. This is a maintainer
        workflow — the updated pins should be committed and released so
        users get the new versions via package upgrade.
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
        from ..security import get_github_level

        config = load_config()
        gh_level = get_github_level(config)
        raw_value = get_setting(config, "github")
        host_auth = has_gh_auth()
        proxy_installed = is_auth_proxy_installed()

        if raw_value == "auto":
            click.echo(f"GitHub level:     auto (effectively {gh_level})")
        else:
            click.echo(f"GitHub level:     {gh_level}")
        click.echo(f"Host gh auth:     {'authenticated' if host_auth else 'not authenticated'}")
        click.echo(f"Auth proxy:       {'installed' if proxy_installed else 'not installed'}")
        if gh_level == "direct":
            click.echo(
                "\nWarning: direct token injection is enabled."
                " Containers get your full GitHub token."
                "\nChange: bubble security set github allowlist-write-graphql"
            )
        elif gh_level == "off":
            click.echo("\nGitHub access is disabled. Enable: bubble security set github auto")
        elif not host_auth:
            click.echo("\nRun 'gh auth login' to authenticate on the host first.")

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

    @config_group.command("show")
    def config_show():
        """Show effective configuration with origin annotations.

        Displays all non-security settings with their effective values,
        annotated to show whether each value is a default or was explicitly
        set in ~/.bubble/config.toml.

        For security settings, use `bubble security` instead.
        """
        # Merge in memory without writing to disk (load_config would
        # create config.toml with defaults as a side effect).
        raw = load_raw_config()
        defaults = copy.deepcopy(DEFAULT_CONFIG)
        config = _deep_merge(defaults, raw)

        # Sections to display (skip security — that's `bubble security`)
        sections = [
            ("editor", "top"),
            ("runtime", "section"),
            ("images", "section"),
            ("network", "section"),
            ("relay", "section"),
            ("remote", "section"),
            ("cloud", "section"),
            ("claude", "section"),
            ("codex", "section"),
            ("tools", "section"),
        ]

        for key, kind in sections:
            if kind == "top":
                # Top-level scalar
                value = config.get(key, "")
                origin = _origin(key, None, config, defaults)
                click.echo(f"{key} = {_format_value(value)}  {origin}")
            else:
                section = config.get(key, {})
                click.echo(f"\n[{key}]")
                if isinstance(section, dict):
                    if not section:
                        click.echo("  (empty)  (default)")
                    else:
                        for subkey, value in section.items():
                            origin = _origin(key, subkey, config, defaults)
                            click.echo(f"  {subkey} = {_format_value(value)}  {origin}")
                elif isinstance(section, list):
                    origin = _origin(key, None, config, defaults)
                    click.echo(f"  {_format_value(section)}  {origin}")

        # Show user-defined sections not in defaults (e.g. [[mounts]])
        for key in raw:
            if key == "security":
                continue
            if key in dict(sections):
                continue
            if key == "editor":
                continue
            value = config.get(key, raw[key])
            if isinstance(value, list) and value and isinstance(value[0], dict):
                # Array of tables (e.g. [[mounts]])
                for item in value:
                    click.echo(f"\n[[{key}]]")
                    for subkey, subval in item.items():
                        click.echo(f"  {subkey} = {_format_value(subval)}")
                click.echo("  (set in config)")
            else:
                click.echo(f"\n[{key}]")
                if isinstance(value, dict):
                    for subkey, subval in value.items():
                        click.echo(f"  {subkey} = {_format_value(subval)}  (set in config)")
                elif isinstance(value, list):
                    for item in value:
                        click.echo(f"  {_format_value(item)}  (set in config)")
                else:
                    click.echo(f"  {_format_value(value)}  (set in config)")

        click.echo(
            "\nSecurity settings are managed separately. "
            "Use `bubble security` to view and configure them."
        )

    @config_group.command("set")
    @click.argument("key")
    @click.argument("value")
    def config_set(key, value):
        """Set a security setting: bubble config set security.<name> <value>.

        Alias for `bubble security set <name> <value>`.
        Setting names use hyphens (e.g. github, claude-credentials).
        Underscores are also accepted as permanent aliases.
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

        save_config(config)
        click.echo(f"Set security.{display_setting_name(name)} = {value}")

    @config_group.command("symlink-claude-projects")
    def config_symlink_claude_projects():
        """Link ~/.bubble/claude-projects/ to ~/.claude/projects/ via symlink.

        If ~/.claude/projects/ is inside a git repo, this command merges any
        existing session data from ~/.bubble/claude-projects/ into
        ~/.claude/projects/ and creates a symlink in its place.

        This lets bubble session state live inside the git-tracked directory
        and get synced across machines automatically.
        """
        from ..config import do_symlink_claude_projects

        if not do_symlink_claude_projects():
            raise SystemExit(1)
