"""Configurable security posture for bubble containers.

Every way in which bubble weakens container isolation is individually
configurable with three settings: auto, on, off.

When set to "auto" (the default), bubble prints a single summary line on
each invocation directing the user to `bubble security` for details.
"on" enables the feature silently, "off" disables it silently.
"""

import os
from dataclasses import dataclass

import click

VALID_VALUES = ("auto", "on", "off")


@dataclass
class SecuritySettingDef:
    """Definition of a configurable security setting."""

    description: str
    auto_default: str  # "on" or "off" — what "auto" acts as
    warning: str  # One-line description of the security trade-off
    category: str  # Grouping category for display


# Categories and their display order
CATEGORIES = [
    ("Network", "Controls what containers can reach over the network"),
    ("Filesystem", "Controls what host paths containers can access"),
    ("Authentication", "Controls credential and API access from containers"),
    ("Cloud", "Controls cloud server provisioning and access"),
    ("SSH", "Controls SSH connection security"),
]

SETTINGS: dict[str, SecuritySettingDef] = {
    "network_github": SecuritySettingDef(
        description="GitHub in network allowlist (exfiltration risk)",
        auto_default="on",
        warning="github.com in allowlist enables data exfiltration",
        category="Network",
    ),
    "shared_cache": SecuritySettingDef(
        description="Writable shared mounts (mathlib cache) across containers",
        auto_default="on",
        warning="mathlib cache is shared read-write across containers",
        category="Filesystem",
    ),
    "user_mounts": SecuritySettingDef(
        description="--mount flag support for arbitrary host paths",
        auto_default="on",
        warning="--mount can expose arbitrary host paths to containers",
        category="Filesystem",
    ),
    "git_manifest_trust": SecuritySettingDef(
        description="Auto-clone dependencies from Lake manifest",
        auto_default="on",
        warning="Lake dependencies auto-cloned from URLs in project manifest",
        category="Filesystem",
    ),
    "claude_credentials": SecuritySettingDef(
        description="Mount ~/.claude credentials into containers",
        auto_default="off",
        warning="credentials give containers access to Claude API auth",
        category="Authentication",
    ),
    "github_auth": SecuritySettingDef(
        description="Repo-scoped GitHub auth via host proxy",
        auto_default="on",
        warning="containers get repo-scoped GitHub push access via auth proxy",
        category="Authentication",
    ),
    "relay": SecuritySettingDef(
        description="Bubble-in-bubble relay daemon",
        auto_default="off",
        warning="relay allows containers to create new bubbles on the host",
        category="Authentication",
    ),
    "cloud_root": SecuritySettingDef(
        description="Cloud server accessed as root with unencrypted key",
        auto_default="on",
        warning="cloud server provisioned/accessed as root with unencrypted SSH key",
        category="Cloud",
    ),
    "host_key_trust": SecuritySettingDef(
        description="StrictHostKeyChecking disabled for container SSH",
        auto_default="on",
        warning="SSH host keys not verified for container connections",
        category="SSH",
    ),
}

# GitHub-related domains that are stripped from the network allowlist
# when network_github is off. Includes API and CDN domains.
GITHUB_DOMAINS = {
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.githubusercontent.com",
    "cli.github.com",
}


def get_setting(config: dict, name: str) -> str:
    """Get the raw value of a security setting (auto/on/off)."""
    if name not in SETTINGS:
        raise ValueError(f"Unknown security setting: {name}")

    return config.get("security", {}).get(name, "auto")


def is_enabled(config: dict, name: str) -> bool:
    """Check if a security feature is effectively enabled.

    Returns True when the setting is "on", or "auto" with auto_default="on".
    """
    value = get_setting(config, name)
    if value == "on":
        return True
    if value == "off":
        return False
    # auto: use the defined default
    return SETTINGS[name].auto_default == "on"


def is_locked_off(config: dict, name: str) -> bool:
    """Check if a feature is explicitly locked off (not just auto -> off)."""
    return get_setting(config, name) == "off"


def has_auto_settings(config: dict) -> bool:
    """Check if any security settings are still on 'auto'."""
    return any(get_setting(config, name) == "auto" for name in SETTINGS)


def print_warnings(config: dict):
    """Print a single summary line when any settings are still 'auto'.

    Suppressed by BUBBLE_QUIET_SECURITY=1 environment variable.
    Once every setting is explicitly configured, no message is shown.
    """
    if os.environ.get("BUBBLE_QUIET_SECURITY") == "1":
        return

    if not has_auto_settings(config):
        return

    click.echo(
        "Note: bubble is using default security assumptions. Review with: bubble security",
        err=True,
    )


def print_security_posture(config: dict):
    """Print the full security posture grouped by category."""
    # Preset commands
    click.echo("Quick presets:")
    click.echo("  bubble security permissive   Enable all conveniences")
    click.echo("  bubble security default      Restore all to auto (defaults)")
    click.echo("  bubble security lockdown     Disable everything risky")
    click.echo()

    auto_count = 0
    for cat_name, cat_desc in CATEGORIES:
        cat_settings = [
            (name, defn) for name, defn in SETTINGS.items() if defn.category == cat_name
        ]
        if not cat_settings:
            continue

        click.echo(f"{cat_name}")
        click.echo(f"  {cat_desc}")
        click.echo()

        for name, defn in cat_settings:
            value = get_setting(config, name)
            if value == "auto":
                auto_count += 1
                effective = defn.auto_default
                status = f"auto (effectively {effective})"
            else:
                effective = value
                status = value

            click.echo(f"  {name}: {status}")
            click.echo(f"    {defn.warning}")
            click.echo(f"    Set: bubble security set {name} on|off|auto")
            click.echo()

    if auto_count == 0:
        click.echo("All settings are explicitly configured. No warnings will be shown.")
    else:
        click.echo(
            f"{auto_count} setting(s) still on 'auto'. "
            "A reminder will be shown on each bubble invocation until all are set."
        )


def apply_preset_permissive(config: dict) -> list[str]:
    """Set all settings to 'on' (enable all conveniences)."""
    config.setdefault("security", {})
    changed = []
    for name in SETTINGS:
        if get_setting(config, name) != "on":
            config["security"][name] = "on"
            changed.append(name)
    return changed


def apply_preset_default(config: dict) -> list[str]:
    """Reset all settings to 'auto'."""
    changed = []
    security = config.get("security", {})
    for name in SETTINGS:
        if security.get(name) is not None:
            del security[name]
            changed.append(name)
    if "security" not in config:
        config["security"] = security
    return changed


def apply_preset_lockdown(config: dict) -> list[str]:
    """Set all settings to 'off' (disable everything risky)."""
    config.setdefault("security", {})
    changed = []
    for name in SETTINGS:
        if get_setting(config, name) != "off":
            config["security"][name] = "off"
            changed.append(name)
    return changed


def filter_github_domains(domains: list[str]) -> list[str]:
    """Remove GitHub-related domains from a domain list."""
    return [d for d in domains if d not in GITHUB_DOMAINS]
