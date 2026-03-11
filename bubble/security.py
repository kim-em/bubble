"""Configurable security posture for bubble containers.

Every way in which bubble weakens container isolation is individually
configurable with three settings: auto, on, off.

When set to "auto" (the default), bubble prints a one-line warning on
each invocation explaining the security trade-off and how to permanently
configure it. "on" enables the feature silently, "off" disables it silently.
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


SETTINGS: dict[str, SecuritySettingDef] = {
    "shared_cache": SecuritySettingDef(
        description="Writable shared mounts (mathlib cache) across containers",
        auto_default="on",
        warning="mathlib cache is shared read-write across containers",
    ),
    "user_mounts": SecuritySettingDef(
        description="--mount flag support for arbitrary host paths",
        auto_default="on",
        warning="--mount can expose arbitrary host paths to containers",
    ),
    "network_github": SecuritySettingDef(
        description="GitHub in network allowlist (exfiltration risk)",
        auto_default="on",
        warning="github.com in allowlist enables data exfiltration",
    ),
    "relay": SecuritySettingDef(
        description="Bubble-in-bubble relay daemon",
        auto_default="off",
        warning="relay allows containers to create new bubbles on the host",
    ),
    "claude_credentials": SecuritySettingDef(
        description="Mount ~/.claude credentials into containers",
        auto_default="off",
        warning="credentials give containers access to Claude API auth",
    ),
    "host_key_trust": SecuritySettingDef(
        description="StrictHostKeyChecking disabled for container SSH",
        auto_default="on",
        warning="SSH host keys not verified for container connections",
    ),
    "cloud_root": SecuritySettingDef(
        description="Cloud server accessed as root with unencrypted key",
        auto_default="on",
        warning="cloud server provisioned/accessed as root with unencrypted SSH key",
    ),
    "git_manifest_trust": SecuritySettingDef(
        description="Auto-clone dependencies from Lake manifest",
        auto_default="on",
        warning="Lake dependencies auto-cloned from URLs in project manifest",
    ),
}

# GitHub-related domains that are stripped from the network allowlist
# when network_github is off.
GITHUB_DOMAINS = {
    "github.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.githubusercontent.com",
}


def get_setting(config: dict, name: str) -> str:
    """Get the raw value of a security setting (auto/on/off).

    Handles backwards compatibility for the relay setting, which was
    previously controlled by [relay] enabled = true/false.
    """
    if name not in SETTINGS:
        raise ValueError(f"Unknown security setting: {name}")

    value = config.get("security", {}).get(name, "auto")

    # Backwards compat: if relay is auto but [relay] enabled = true,
    # treat as "on" (user explicitly enabled via old config).
    if name == "relay" and value == "auto":
        if config.get("relay", {}).get("enabled", False):
            return "on"

    return value


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


def print_warnings(config: dict):
    """Print security warnings for all auto settings to stderr.

    Suppressed by BUBBLE_QUIET_SECURITY=1 environment variable.
    """
    if os.environ.get("BUBBLE_QUIET_SECURITY") == "1":
        return

    for name, defn in SETTINGS.items():
        value = get_setting(config, name)
        if value != "auto":
            continue

        effective = defn.auto_default
        if effective == "on":
            action = f"Lock: bubble config set security.{name} off"
        else:
            action = f"Enable: bubble config set security.{name} on"

        click.echo(
            f"\u26a0 {name}=auto ({effective}): {defn.warning}. {action}",
            err=True,
        )


def filter_github_domains(domains: list[str]) -> list[str]:
    """Remove GitHub-related domains from a domain list."""
    return [d for d in domains if d not in GITHUB_DOMAINS]
