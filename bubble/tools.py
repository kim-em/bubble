"""Pluggable tool installation system for container images.

Tool install scripts (in images/scripts/tools/) MUST be idempotent.
They may be run both during image builds and directly in running containers
(when the base image doesn't have the latest tools baked in yet).
"""

import hashlib
import shutil
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent / "images" / "scripts" / "tools"

# Registry of available tools.
# Each entry maps a tool name to:
#   script: filename in bubble/images/scripts/tools/
#   host_cmd: command to check on host for "auto" detection
#   network_domains: extra domains needed during install
TOOLS = {
    "claude": {
        "script": "claude.sh",
        "host_cmd": "claude",
        "network_domains": ["registry.npmjs.org", "deb.nodesource.com"],
    },
    "codex": {
        "script": "codex.sh",
        "host_cmd": "codex",
        "network_domains": ["registry.npmjs.org", "deb.nodesource.com"],
    },
    "gh": {
        "script": "gh.sh",
        "host_cmd": "gh",
        "network_domains": ["cli.github.com"],
    },
}


def available_tools() -> list[str]:
    """Return sorted list of available tool names."""
    return sorted(TOOLS.keys())


def _host_has_command(cmd: str) -> bool:
    """Check if a command is available on the host."""
    return shutil.which(cmd) is not None


def resolve_tools(config: dict) -> list[str]:
    """Resolve which tools should be installed based on config.

    Returns sorted list of tool names that should be installed.
    Each tool's config value is "yes", "no", or "auto" (default).
    "auto" installs the tool if the corresponding command is found on the host.
    """
    tools_config = config.get("tools", {})
    enabled = []
    for name, spec in sorted(TOOLS.items()):
        setting = tools_config.get(name, "auto")
        if setting == "yes":
            enabled.append(name)
        elif setting == "auto":
            if _host_has_command(spec["host_cmd"]):
                enabled.append(name)
        # "no" -> skip
    return enabled


def tools_hash(enabled_tools: list[str]) -> str:
    """Compute a stable hash of the enabled tool set and their scripts.

    Includes both tool names and script contents so that changes to
    install scripts also trigger rebuilds.
    """
    h = hashlib.sha256()
    for name in sorted(enabled_tools):
        h.update(name.encode())
        h.update(b"\x00")
        script_path = SCRIPTS_DIR / TOOLS[name]["script"]
        if script_path.exists():
            h.update(script_path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def tool_script(name: str) -> str:
    """Read the install script for a tool."""
    spec = TOOLS[name]
    return (SCRIPTS_DIR / spec["script"]).read_text()


def tool_network_domains(enabled_tools: list[str]) -> list[str]:
    """Return extra network domains needed to install the given tools."""
    domains = []
    for name in enabled_tools:
        for d in TOOLS[name].get("network_domains", []):
            if d not in domains:
                domains.append(d)
    return domains


def combined_tool_script(enabled_tools: list[str]) -> str | None:
    """Build a combined install script for all enabled tools.

    Returns None if no tools are enabled.
    """
    if not enabled_tools:
        return None
    parts = ["#!/bin/bash", "set -euo pipefail", ""]
    for name in sorted(enabled_tools):
        parts.append(f"# --- Install {name} ---")
        parts.append(tool_script(name))
        parts.append("")
    return "\n".join(parts)
