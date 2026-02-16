"""VSCode Remote SSH integration."""

import re
import shlex
import subprocess
from pathlib import Path

# Valid bubble name pattern (alphanumeric + hyphens, starts with letter)
_BUBBLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Domains VSCode Remote SSH needs to function (marketplace for extensions)
VSCODE_NETWORK_DOMAINS = [
    "marketplace.visualstudio.com",
    "*.gallery.vsassets.io",
    "update.code.visualstudio.com",
    "*.vo.msecnd.net",
]

SSH_CONFIG_DIR = Path.home() / ".ssh" / "config.d"
SSH_CONFIG_FILE = SSH_CONFIG_DIR / "bubble"
SSH_MAIN_CONFIG = Path.home() / ".ssh" / "config"


def add_ssh_config(bubble_name: str, user: str = "user", remote_host=None):
    """Add an SSH config entry for a bubble.

    Uses `incus exec` as ProxyCommand to avoid port forwarding issues on macOS.
    When remote_host is provided, chains SSH through the remote host to reach
    the container.

    Args:
        bubble_name: Container name.
        user: User inside the container.
        remote_host: Optional RemoteHost for chained ProxyCommand.
    """
    if not _BUBBLE_NAME_RE.match(bubble_name):
        raise ValueError(f"Invalid bubble name for SSH config: {bubble_name!r}")
    SSH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    incus_cmd = f'incus exec {bubble_name} -- su - {user} -c "nc localhost 22"'

    if remote_host is not None:
        port_args = f"-p {remote_host.port} " if remote_host.port != 22 else ""
        dest = shlex.quote(remote_host.ssh_destination)
        proxy_cmd = f"ssh {port_args}{dest} {incus_cmd}"
    else:
        proxy_cmd = incus_cmd

    entry = f"""
Host bubble-{bubble_name}
  User {user}
  ProxyCommand {proxy_cmd}
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


def open_editor(
    editor: str,
    bubble_name: str,
    remote_path: str = "/home/user",
    workspace_file: str | None = None,
    command: list[str] | None = None,
):
    """Open the specified editor connected to a bubble.

    If command is provided (only valid with editor="shell"), runs that command
    via SSH instead of opening an interactive session.
    """
    if editor == "vscode":
        open_vscode(bubble_name, remote_path, workspace_file=workspace_file)
    elif editor == "shell":
        ssh_cmd = ["ssh", f"bubble-{bubble_name}"]
        if command:
            ssh_cmd += command
        subprocess.run(ssh_cmd)


def open_vscode(
    bubble_name: str,
    remote_path: str = "/home/user",
    workspace_file: str | None = None,
):
    """Open VSCode connected to a bubble via Remote SSH."""
    host = f"bubble-{bubble_name}"
    if workspace_file:
        uri = f"vscode-remote://ssh-remote+{host}{workspace_file}"
        flag = "--file-uri"
    else:
        uri = f"vscode-remote://ssh-remote+{host}{remote_path}"
        flag = "--folder-uri"
    try:
        subprocess.run(["code", "--disable-workspace-trust", flag, uri], check=True)
    except subprocess.CalledProcessError:
        if workspace_file:
            # Fall back to opening the folder if --file-uri fails
            folder_uri = f"vscode-remote://ssh-remote+{host}{remote_path}"
            try:
                subprocess.run(
                    ["code", "--disable-workspace-trust", "--folder-uri", folder_uri],
                    check=True,
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass
    except FileNotFoundError:
        print(f"VSCode CLI not found. Connect manually: Remote SSH → {host}")
        print(f"Or run: code {flag} {uri}")


