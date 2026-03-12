"""Pluggable tool installation system for container images.

Tool install scripts (in images/scripts/tools/) are run during base image
builds. When the tool configuration changes, the base image is rebuilt
synchronously before any new containers are created.
"""

import hashlib
import json
import shlex
import shutil
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent / "images" / "scripts" / "tools"
PINS_FILE = SCRIPTS_DIR / "pins.json"

# Registry of available tools.
# Each entry maps a tool name to:
#   script: filename in bubble/images/scripts/tools/
#   host_cmd: command to check on host for "auto" detection
#   network_domains: extra domains needed during install
#   runtime_domains: domains needed at runtime (added to container firewall)
#   priority: install order (lower = first). Language tools before editors.
TOOLS = {
    "claude": {
        "script": "claude.sh",
        "host_cmd": "claude",
        "network_domains": ["registry.npmjs.org", "nodejs.org"],
        "runtime_domains": ["api.anthropic.com"],
        "priority": 50,
    },
    "codex": {
        "script": "codex.sh",
        "host_cmd": "codex",
        "network_domains": ["registry.npmjs.org", "nodejs.org"],
        "runtime_domains": ["api.openai.com"],
        "priority": 50,
    },
    "elan": {
        "script": "elan.sh",
        "host_cmd": "elan",
        "network_domains": [
            "raw.githubusercontent.com",
            "api.github.com",
            "github.com",
        ],
        "runtime_domains": [],
        "priority": 10,
    },
    "gh": {
        "script": "gh.sh",
        "host_cmd": "gh",
        "network_domains": ["cli.github.com"],
        "runtime_domains": [],
        "priority": 50,
    },
    "emacs": {
        "script": "emacs.sh",
        "host_cmd": "emacs",
        "network_domains": [],
        "runtime_domains": [],
        "priority": 90,
    },
    "neovim": {
        "script": "neovim.sh",
        "host_cmd": "nvim",
        "network_domains": [],
        "runtime_domains": [],
        "priority": 90,
    },
    "vscode": {
        "script": "vscode.sh",
        "host_cmd": "code",
        "network_domains": [
            "marketplace.visualstudio.com",
            "*.gallery.vsassets.io",
            "update.code.visualstudio.com",
            "*.vo.msecnd.net",
        ],
        "runtime_domains": [
            "marketplace.visualstudio.com",
            "*.gallery.vsassets.io",
            "update.code.visualstudio.com",
            "*.vo.msecnd.net",
        ],
        "priority": 90,
    },
}

# Editor tools — only one is enabled at a time, based on the "editor" config key.
EDITOR_TOOLS = {"vscode", "emacs", "neovim"}


def load_pins() -> dict:
    """Load pinned versions and checksums from pins.json."""
    return json.loads(PINS_FILE.read_text())


def save_pins(pins: dict):
    """Write pinned versions and checksums to pins.json."""
    PINS_FILE.write_text(json.dumps(pins, indent=2) + "\n")


def _pins_preamble() -> str:
    """Generate shell export statements for all pinned versions."""
    pins = load_pins()
    lines = []
    for key, value in sorted(pins.items()):
        lines.append(f"export {key}={shlex.quote(str(value))}")
    return "\n".join(lines)


def available_tools() -> list[str]:
    """Return sorted list of available tool names."""
    return sorted(TOOLS.keys())


def _host_has_command(cmd: str) -> bool:
    """Check if a command is available on the host."""
    return shutil.which(cmd) is not None


def _sort_by_priority(tools: list[str]) -> list[str]:
    """Sort tool names by (priority, name) for deterministic install order."""
    return sorted(tools, key=lambda n: (TOOLS[n].get("priority", 50), n))


def resolve_tools(config: dict) -> list[str]:
    """Resolve which tools should be installed based on config.

    Returns list of tool names sorted by priority.
    Each tool's config value is "yes", "no", or "auto" (default).
    "auto" installs the tool if the corresponding command is found on the host.

    Editor tools (vscode, emacs, neovim) are special: the configured editor
    (from the "editor" config key, default "vscode") is treated as "yes"
    unless explicitly set to "no" in [tools]. Other editors are skipped
    unless force-enabled with tools.<editor> = "yes". Per-invocation editor
    overrides (--emacs, --neovim, --shell) control which editor is launched
    but do not change which is installed in the image.
    """
    tools_config = config.get("tools", {})
    editor = config.get("editor", "vscode")
    enabled = []
    for name, spec in sorted(TOOLS.items()):
        # Editor tools use the "editor" config key, not "auto" detection
        if name in EDITOR_TOOLS:
            if name == editor and tools_config.get(name) != "no":
                enabled.append(name)
            elif tools_config.get(name) == "yes":
                enabled.append(name)
            continue
        setting = tools_config.get(name, "auto")
        if setting == "yes":
            enabled.append(name)
        elif setting == "auto":
            if _host_has_command(spec["host_cmd"]):
                enabled.append(name)
        # "no" -> skip
    return _sort_by_priority(enabled)


def tools_hash(enabled_tools: list[str]) -> str:
    """Compute a stable hash of the enabled tool set, their scripts, and pins.

    Includes tool names, script contents, and pinned versions so that changes
    to install scripts or version pins trigger rebuilds.
    """
    h = hashlib.sha256()
    # Include pins so version bumps trigger rebuilds
    pins = load_pins()
    h.update(json.dumps(pins, sort_keys=True).encode())
    h.update(b"\x00")
    for name in _sort_by_priority(enabled_tools):
        h.update(name.encode())
        h.update(b"\x00")
        script_path = SCRIPTS_DIR / TOOLS[name]["script"]
        if script_path.exists():
            h.update(script_path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def tool_script(name: str) -> str:
    """Read the install script for a tool with pinned versions injected."""
    spec = TOOLS[name]
    script = (SCRIPTS_DIR / spec["script"]).read_text()
    preamble = _pins_preamble()
    return preamble + "\n" + script


def tool_network_domains(enabled_tools: list[str]) -> list[str]:
    """Return extra network domains needed to install the given tools."""
    domains = []
    for name in enabled_tools:
        for d in TOOLS[name].get("network_domains", []):
            if d not in domains:
                domains.append(d)
    return domains


def tool_runtime_domains(enabled_tools: list[str]) -> list[str]:
    """Return network domains needed at runtime by the given tools.

    These domains are added to the container's firewall allowlist for the
    lifetime of the container, unlike network_domains which are only
    available during installation.
    """
    domains = []
    for name in enabled_tools:
        for d in TOOLS[name].get("runtime_domains", []):
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
    for name in _sort_by_priority(enabled_tools):
        parts.append(f"# --- Install {name} ---")
        parts.append(tool_script(name))
        parts.append("")
    return "\n".join(parts)


def fetch_latest_pins() -> dict:
    """Fetch the latest versions and checksums from upstream sources.

    Returns a new pins dict with updated values.
    Requires network access to nodejs.org and npmjs.org.
    """
    import re
    import urllib.request

    pins = {}

    # Node.js: latest v22 LTS
    shasums = (
        urllib.request.urlopen("https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt")
        .read()
        .decode()
    )
    version = None
    for line in shasums.splitlines():
        if "linux-x64.tar.xz" in line and not line.strip().startswith("#"):
            sha, fname = line.split()
            pins["NODE_SHA256_X64"] = sha
            m = re.search(r"node-v([\d.]+)-", fname)
            if m:
                version = m.group(1)
        if "linux-arm64.tar.xz" in line and not line.strip().startswith("#"):
            sha, _ = line.split()
            pins["NODE_SHA256_ARM64"] = sha
    if version:
        pins["NODE_VERSION"] = version

    # Claude Code: latest npm version
    data = json.loads(
        urllib.request.urlopen("https://registry.npmjs.org/@anthropic-ai/claude-code/latest").read()
    )
    pins["CLAUDE_CODE_VERSION"] = data["version"]

    # Codex: latest npm version
    data = json.loads(
        urllib.request.urlopen("https://registry.npmjs.org/@openai/codex/latest").read()
    )
    pins["CODEX_VERSION"] = data["version"]

    # Validate that all required keys were found
    required = {
        "NODE_VERSION",
        "NODE_SHA256_X64",
        "NODE_SHA256_ARM64",
        "CLAUDE_CODE_VERSION",
        "CODEX_VERSION",
    }
    missing = required - set(pins.keys())
    if missing:
        raise RuntimeError(f"Failed to fetch pins for: {', '.join(sorted(missing))}")

    return pins
