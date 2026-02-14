"""VSCode Remote SSH integration."""

import json
import platform
import re
import subprocess
from pathlib import Path

# Valid bubble name pattern (alphanumeric + hyphens, starts with letter)
_BUBBLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

SSH_CONFIG_DIR = Path.home() / ".ssh" / "config.d"
SSH_CONFIG_FILE = SSH_CONFIG_DIR / "bubble"
SSH_MAIN_CONFIG = Path.home() / ".ssh" / "config"

# Port range for SSH forwarding (one per bubble)
SSH_PORT_BASE = 22100


def _allocated_ports() -> set[int]:
    """Read already-allocated ports from the SSH config file."""
    ports = set()
    if SSH_CONFIG_FILE.exists():
        for line in SSH_CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("Port "):
                try:
                    ports.add(int(line.split()[1]))
                except (IndexError, ValueError):
                    pass
    return ports


def allocate_port() -> int:
    """Find the next available SSH port."""
    used = _allocated_ports()
    port = SSH_PORT_BASE
    while port in used:
        port += 1
    return port


def add_ssh_config(bubble_name: str, port: int = 0, user: str = "user"):
    """Add an SSH config entry for a bubble.

    Uses `incus exec` as ProxyCommand to avoid port forwarding issues on macOS.
    """
    if not _BUBBLE_NAME_RE.match(bubble_name):
        raise ValueError(f"Invalid bubble name for SSH config: {bubble_name!r}")
    SSH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    entry = f"""
Host bubble-{bubble_name}
  User {user}
  ProxyCommand incus exec {bubble_name} -- su - {user} -c "nc localhost 22"
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
"""
    # Append to config file
    with open(SSH_CONFIG_FILE, "a") as f:
        f.write(entry)

    _ensure_include_directive()


def remove_ssh_config(bubble_name: str):
    """Remove an SSH config entry for a bubble."""
    if not SSH_CONFIG_FILE.exists():
        return

    lines = SSH_CONFIG_FILE.read_text().splitlines()
    result = []
    skip = False
    for line in lines:
        if line.strip() == f"Host bubble-{bubble_name}":
            skip = True
            continue
        if skip and line.strip().startswith("Host "):
            skip = False
        if not skip:
            result.append(line)

    SSH_CONFIG_FILE.write_text("\n".join(result) + "\n" if result else "")


def _ensure_include_directive():
    """Ensure ~/.ssh/config includes our config.d directory."""
    ssh_config = SSH_MAIN_CONFIG
    include_line = f"Include {SSH_CONFIG_DIR}/*"

    if ssh_config.exists():
        content = ssh_config.read_text()
        if include_line in content:
            return
        # Prepend the include (must be at top of ssh config)
        ssh_config.write_text(include_line + "\n\n" + content)
    else:
        ssh_config.parent.mkdir(parents=True, exist_ok=True)
        ssh_config.write_text(include_line + "\n")


def _vscode_settings_path() -> Path:
    """Get the VSCode user settings.json path."""
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json"
    return Path.home() / ".config" / "Code" / "User" / "settings.json"


def ensure_vscode_extensions(extensions: list[str] | None = None):
    """Ensure VSCode is configured to auto-install extensions on remote SSH hosts."""
    if not extensions:
        return

    settings_path = _vscode_settings_path()
    if not settings_path.exists():
        return

    try:
        settings = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    existing = settings.get("remote.SSH.defaultExtensions", [])
    added = []
    for ext in extensions:
        if ext not in existing:
            existing.append(ext)
            added.append(ext)

    if added:
        settings["remote.SSH.defaultExtensions"] = existing
        settings_path.write_text(json.dumps(settings, indent=4) + "\n")


def open_vscode(bubble_name: str, remote_path: str = "/home/user"):
    """Open VSCode connected to a bubble via Remote SSH."""
    host = f"bubble-{bubble_name}"
    uri = f"vscode-remote://ssh-remote+{host}{remote_path}"
    try:
        subprocess.run(["code", "--folder-uri", uri], check=True)
    except FileNotFoundError:
        print(f"VSCode CLI not found. Connect manually: Remote SSH → {host}")
        print(f"Or run: code --folder-uri {uri}")
