"""Configurable security posture for bubble containers.

Every way in which bubble weakens container isolation is individually
configurable with three settings: auto, on, off.

When set to "auto" (the default), bubble prints a single summary line on
each invocation directing the user to `bubble security` for details.
"on" enables the feature silently, "off" disables it silently.

The `github` setting is special: instead of auto/on/off, it uses a
graduated access level (off, basic, rest, allowlist-read-graphql,
allowlist-write-graphql, write-graphql, direct). `auto` defaults to
`allowlist-write-graphql`.
"""

import os
from dataclasses import dataclass

import click

VALID_VALUES = ("auto", "on", "off")

# Graduated GitHub access levels, from most restrictive to most permissive.
# Each level is a strict superset of the one above it.
GITHUB_LEVELS = (
    "off",
    "basic",
    "rest",
    "allowlist-read-graphql",
    "allowlist-write-graphql",
    "write-graphql",
    "direct",
)

# Descriptions for each GitHub level (used by `bubble security` display)
GITHUB_LEVEL_DESCRIPTIONS = {
    "off": "no GitHub access at all",
    "basic": "git push/pull only (proxy rewrites, repo-scoped)",
    "rest": "+ repo-scoped REST API (GET/POST/PATCH/DELETE /repos/{owner}/{repo}/...)",
    "allowlist-read-graphql": "+ allowlisted GraphQL queries (implies rest)",
    "allowlist-write-graphql": "+ allowlisted GraphQL mutations (implies allowlist-read-graphql)",
    "write-graphql": "+ arbitrary GraphQL, no allowlist filtering (implies rest)",
    "direct": "inject the raw token into the container, no proxy at all",
}

# The default GitHub level when set to "auto"
GITHUB_AUTO_DEFAULT = "allowlist-write-graphql"

# Legacy setting names that were replaced by the unified `github` setting
_LEGACY_GITHUB_KEYS = ("github_auth", "github_api", "github_token_inject")


def valid_values_for(name: str) -> tuple[str, ...]:
    """Return the valid values for a security setting.

    Most settings accept auto/on/off. Some (like github) accept
    additional values.
    """
    defn = SETTINGS.get(name)
    if defn and defn.extra_values:
        return VALID_VALUES + defn.extra_values
    return VALID_VALUES


def normalize_setting_name(name: str) -> str:
    """Normalize a setting name: hyphens -> underscores for internal lookup.

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
    auto_default: str  # "on" or "off" -- what "auto" acts as
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
    "shared_cache": SecuritySettingDef(
        description="Shared mounts (mathlib cache) across containers",
        auto_default="on",
        warning=(
            "mathlib cache is shared read-write across containers;"
            " a compromised container could poison cached artifacts"
        ),
        category="Filesystem",
        extra_values=("overlay",),
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
    "github": SecuritySettingDef(
        description="GitHub access level for containers",
        auto_default="on",  # not "off"; actual level resolved by get_github_level()
        warning="containers get repo-scoped GitHub access via auth proxy",
        category="Authentication",
        extra_values=GITHUB_LEVELS,
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


def get_setting(config: dict, name: str) -> str:
    """Get the validated value of a security setting.

    Returns auto/on/off (or extra values like github levels).
    Unrecognized values from hand-edited config are treated as "auto"
    (fail-closed: typos don't escalate access).
    """
    if name not in SETTINGS:
        raise ValueError(f"Unknown security setting: {name}")

    value = config.get("security", {}).get(name, "auto")

    # Validate: reject unrecognized values (typos, corruption) by
    # falling back to "auto". This prevents e.g. github = "readwrtie"
    # from being treated as enabled.
    allowed = valid_values_for(name)
    if value not in allowed:
        return "auto"

    return value


def is_enabled(config: dict, name: str) -> bool:
    """Check if a security feature is effectively enabled.

    Returns True when the setting is "on" (or any extra value like
    "overlay" or github levels other than "off"), or "auto" with
    auto_default="on".
    """
    value = get_setting(config, name)
    if value == "off":
        return False
    if value == "auto":
        return SETTINGS[name].auto_default == "on"
    # "on", "overlay", github levels, etc. count as enabled
    return True


def is_locked_off(config: dict, name: str) -> bool:
    """Check if a feature is explicitly locked off (not just auto -> off)."""
    return get_setting(config, name) == "off"


def _migrate_legacy_github(security: dict) -> str:
    """Map old github_auth/github_api/github_token_inject to a unified level.

    Called when old keys are present in the security section but the new
    'github' key has not been set.
    """
    auth = security.get("github_auth", "auto")
    api = security.get("github_api", "auto")
    inject = security.get("github_token_inject", "auto")

    # Token injection overrides everything — in the old code, token
    # injection was checked independently of github_auth, so
    # {github_auth=off, github_token_inject=on} meant "direct".
    if inject == "on":
        return "direct"

    # If auth is off, nothing works
    if auth == "off":
        return "off"

    # Auth is on/auto (effectively on). Check API level.
    if api == "off":
        return "basic"
    if api == "read-write":
        return "write-graphql"
    # api == "on" or "auto" (default)
    return "allowlist-write-graphql"


def get_github_level(config: dict) -> str:
    """Resolve the effective GitHub access level.

    Returns one of the GITHUB_LEVELS values (never "auto" or "on").
    Handles migration from old github_auth/github_api/github_token_inject keys.
    """
    value = get_setting(config, "github")

    if value == "auto":
        # Check for legacy settings and migrate
        security = config.get("security", {})
        if any(k in security for k in _LEGACY_GITHUB_KEYS):
            return _migrate_legacy_github(security)
        return GITHUB_AUTO_DEFAULT

    if value == "on":
        # Treat "on" the same as auto default
        return GITHUB_AUTO_DEFAULT

    # Explicit level (off, basic, rest, ..., direct)
    return value


def warn_legacy_github_settings(config: dict, notices=None):
    """Print deprecation warnings if old github_auth/api/inject keys are found."""
    security = config.get("security", {})
    old_keys = [k for k in _LEGACY_GITHUB_KEYS if k in security]
    if not old_keys:
        return

    # If the user has already set the new 'github' key explicitly,
    # the legacy keys are inert — skip the warning to avoid suggesting
    # overwriting the user's explicit new setting with a legacy-derived value.
    if "github" in security:
        return

    migrated = _migrate_legacy_github(security)
    names = ", ".join(display_setting_name(k) for k in old_keys)
    if notices:
        notices.begin()
    click.echo(
        f"Deprecated: {names} replaced by unified 'github' setting.",
        err=True,
    )
    click.echo(
        f"  Current mapping: github = {migrated}",
        err=True,
    )
    click.echo(
        f"  Migrate: bubble security set github {migrated}",
        err=True,
    )


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
    Also prints deprecation warnings for legacy github settings.
    """
    if os.environ.get("BUBBLE_QUIET_SECURITY") == "1":
        return

    warn_legacy_github_settings(config, notices=notices)

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

            if name == "github":
                # Special display for the graduated github setting
                _print_github_posture(config, value, display)
                if value == "auto":
                    auto_count += 1
                click.echo()
                continue

            if value == "auto":
                auto_count += 1
                effective = defn.auto_default
                status = f"auto (effectively {effective})"
            else:
                effective = value
                status = value

            click.echo(f"  {display}: {status}")
            if value == "overlay" and name == "shared_cache":
                click.echo("    shared cache is read-only with per-container writable overlay")
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


def _print_github_posture(config: dict, raw_value: str, display: str):
    """Print the github setting posture with all level descriptions."""
    level = get_github_level(config)
    if raw_value == "auto":
        status = f"auto (effectively {level})"
    else:
        status = raw_value

    click.echo(f"  {display}: {status}")
    click.echo()
    click.echo("    Levels (each includes all above):")
    for lvl in GITHUB_LEVELS:
        marker = ">>>" if lvl == level else "   "
        click.echo(f"    {marker} {lvl}: {GITHUB_LEVEL_DESCRIPTIONS[lvl]}")
    click.echo()
    click.echo(f"    Set: bubble security set {display} <level>")


def apply_preset_permissive(config: dict) -> list[str]:
    """Set all settings to their most permissive value.

    For most settings this is "on". For the github setting this is "direct".
    Does not downgrade settings that have a stronger explicit value
    (e.g. shared_cache = "overlay" is preserved, not reset to "on").
    """
    config.setdefault("security", {})
    changed = []
    for name, defn in SETTINGS.items():
        current = get_setting(config, name)
        if name == "github":
            if current != "direct":
                config["security"][name] = "direct"
                changed.append(name)
            continue
        # Skip if already "on" or a stronger explicit extra value
        if current == "on" or (defn.extra_values and current in defn.extra_values):
            continue
        config["security"][name] = "on"
        changed.append(name)
    # Clean up any legacy keys
    _remove_legacy_keys(config)
    return changed


def apply_preset_default(config: dict) -> list[str]:
    """Reset all settings to 'auto'."""
    changed = []
    security = config.get("security", {})
    for name in SETTINGS:
        if security.get(name) is not None:
            del security[name]
            changed.append(name)
    # Also clean up legacy keys
    for key in _LEGACY_GITHUB_KEYS:
        if security.get(key) is not None:
            del security[key]
            if key not in changed:
                changed.append(key)
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
    # Clean up any legacy keys
    _remove_legacy_keys(config)
    return changed


def _remove_legacy_keys(config: dict):
    """Remove old github_auth/github_api/github_token_inject keys from config."""
    security = config.get("security", {})
    for key in _LEGACY_GITHUB_KEYS:
        security.pop(key, None)


def github_domains_for_allowlist(domains: list[str]) -> list[str]:
    """GitHub-related domains that appear in a domain list.

    Used to strip direct GitHub network access when traffic should go
    through the auth proxy instead.  Case-insensitive to prevent bypass
    via mixed-case domains.
    """
    github_suffixes = (".github.com", ".githubusercontent.com")
    return [
        d
        for d in domains
        if d.lower() == "github.com"
        or d.lower() == "cli.github.com"
        or any(d.lower().endswith(s) for s in github_suffixes)
    ]


def filter_github_domains(domains: list[str]) -> list[str]:
    """Remove GitHub-related domains from a domain list."""
    gh = set(github_domains_for_allowlist(domains))
    return [d for d in domains if d not in gh]
