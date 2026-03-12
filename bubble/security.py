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


def valid_values_for(name: str) -> tuple[str, ...]:
    """Return the valid values for a security setting.

    Most settings accept auto/on/off. Some (like github_api) accept
    additional values.
    """
    defn = SETTINGS.get(name)
    if defn and defn.extra_values:
        return VALID_VALUES + defn.extra_values
    return VALID_VALUES


def normalize_setting_name(name: str) -> str:
    """Normalize a setting name: hyphens → underscores for internal lookup.

    Both hyphens and underscores are permanently accepted at the CLI edge.
    Internally, canonical names always use underscores.
    """
    return name.replace("-", "_")


def display_setting_name(name: str) -> str:
    """Return the display form of a setting name (hyphens for CLI consistency)."""
    return name.replace("_", "-")


@dataclass
class SecuritySettingDef:
    """Definition of a configurable security setting."""

    description: str
    auto_default: str  # "on" or "off" — what "auto" acts as
    warning: str  # One-line description of the security trade-off
    category: str  # Grouping category for display
    extra_values: tuple[str, ...] = ()  # Additional valid values beyond auto/on/off


# Categories and their display order
CATEGORIES = [
    ("Network", "Controls what containers can reach over the network"),
    ("Filesystem", "Controls what host paths containers can access"),
    ("Authentication", "Controls credential and API access from containers"),
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
        auto_default="on",
        warning="credentials give containers access to Claude API auth",
        category="Authentication",
    ),
    "codex_credentials": SecuritySettingDef(
        description="Mount ~/.codex credentials into containers",
        auto_default="on",
        warning="credentials give containers access to OpenAI/Codex API auth",
        category="Authentication",
    ),
    "github_auth": SecuritySettingDef(
        description="Repo-scoped GitHub auth via host proxy",
        auto_default="on",
        warning="containers get repo-scoped GitHub push access via auth proxy",
        category="Authentication",
    ),
    "github_api": SecuritySettingDef(
        description="GitHub API access via auth proxy (gh CLI, REST, GraphQL)",
        auto_default="on",
        warning="containers get read-only GitHub API access (account-wide reads via GraphQL)",
        category="Authentication",
        extra_values=("read-write",),
    ),
    "github_token_inject": SecuritySettingDef(
        description="Direct GitHub token injection into container (bypasses proxy)",
        auto_default="off",
        warning="containers get the host's full GitHub token (unrestricted access)",
        category="Authentication",
    ),
    "relay": SecuritySettingDef(
        description="Bubble-in-bubble relay daemon",
        auto_default="on",
        warning="relay allows containers to create new bubbles on the host",
        category="Authentication",
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
    """Get the validated value of a security setting.

    Returns auto/on/off (or extra values like read-write for github_api).
    Unrecognized values from hand-edited config are treated as "auto"
    (fail-closed: typos don't escalate access).
    """
    if name not in SETTINGS:
        raise ValueError(f"Unknown security setting: {name}")

    value = config.get("security", {}).get(name, "auto")

    # Validate: reject unrecognized values (typos, corruption) by
    # falling back to "auto". This prevents e.g. github_api = "readwrtie"
    # from being treated as enabled.
    allowed = valid_values_for(name)
    if value not in allowed:
        return "auto"

    return value


def is_enabled(config: dict, name: str) -> bool:
    """Check if a security feature is effectively enabled.

    Returns True when the setting is "on" (or any extra value like
    "read-write"), or "auto" with auto_default="on".
    """
    value = get_setting(config, name)
    if value == "off":
        return False
    if value == "auto":
        return SETTINGS[name].auto_default == "on"
    # "on", "read-write", or any other extra value counts as enabled
    return True


def is_locked_off(config: dict, name: str) -> bool:
    """Check if a feature is explicitly locked off (not just auto -> off)."""
    return get_setting(config, name) == "off"


def should_include_credentials(requested: bool, config: dict, setting_name: str) -> bool:
    """Resolve whether credentials should be included.

    Locked-off always wins.  Otherwise, include if the resolved requested
    flag is True or the security setting enables them.
    """
    if is_locked_off(config, setting_name):
        return False
    return requested or is_enabled(config, setting_name)


def has_auto_settings(config: dict) -> bool:
    """Check if any security settings are still on 'auto'."""
    return any(get_setting(config, name) == "auto" for name in SETTINGS)


def print_warnings(config: dict, notices=None):
    """Print a single summary line when any settings are still 'auto'.

    Suppressed by BUBBLE_QUIET_SECURITY=1 environment variable.
    Once every setting is explicitly configured, no message is shown.
    """
    if os.environ.get("BUBBLE_QUIET_SECURITY") == "1":
        return

    if not has_auto_settings(config):
        return

    if notices:
        notices.begin()
    click.echo(
        "Note: bubble is using default security assumptions. Review with: bubble security",
        err=True,
    )


def print_security_posture(config: dict):
    """Print the full security posture grouped by category."""
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
            display = display_setting_name(name)
            if value == "auto":
                auto_count += 1
                effective = defn.auto_default
                status = f"auto (effectively {effective})"
            else:
                effective = value
                status = value

            click.echo(f"  {display}: {status}")
            if value == "read-write" and name == "github_api":
                click.echo(
                    "    containers get read-write GitHub API access"
                    " (mutations, PR comments, issue management)"
                )
            else:
                click.echo(f"    {defn.warning}")
            values_hint = "|".join(valid_values_for(name))
            click.echo(f"    Set: bubble security set {display} {values_hint}")
            click.echo()

    if auto_count == 0:
        click.echo("All settings are explicitly configured. No warnings will be shown.")
    else:
        click.echo(
            f"{auto_count} setting(s) still on 'auto'. "
            "A reminder will be shown on each bubble invocation until all are set."
        )

    # Preset commands at the bottom so current state is visible first
    click.echo()
    click.echo("Quick presets:")
    click.echo("  bubble security permissive   Enable all conveniences")
    click.echo("  bubble security default      Restore all to auto (defaults)")
    click.echo("  bubble security lockdown     Disable everything risky")


def apply_preset_permissive(config: dict) -> list[str]:
    """Set all settings to 'on' (enable all conveniences).

    Does not downgrade settings that have a stronger explicit value
    (e.g. github_api = "read-write" is preserved, not reset to "on").
    """
    config.setdefault("security", {})
    changed = []
    for name in SETTINGS:
        current = get_setting(config, name)
        # Skip if already "on" or a stronger explicit extra value
        defn = SETTINGS[name]
        if current == "on" or (defn.extra_values and current in defn.extra_values):
            continue
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
