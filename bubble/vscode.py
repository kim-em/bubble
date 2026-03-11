"""VSCode Remote SSH integration."""

import re
import shlex
import subprocess
from pathlib import Path

# Valid bubble name pattern (alphanumeric + hyphens, starts with letter)
_BUBBLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

SSH_CONFIG_DIR = Path.home() / ".ssh" / "config.d"
SSH_CONFIG_FILE = SSH_CONFIG_DIR / "bubble"
SSH_MAIN_CONFIG = Path.home() / ".ssh" / "config"


def add_ssh_config(
    bubble_name: str, user: str = "user", remote_host=None, host_key_trust: bool = True
):
    """Add an SSH config entry for a bubble.

    Uses `incus exec` as ProxyCommand to avoid port forwarding issues on macOS.
    When remote_host is provided, chains SSH through the remote host to reach
    the container.

    Args:
        bubble_name: Container name.
        user: User inside the container.
        remote_host: Optional RemoteHost for chained ProxyCommand.
        host_key_trust: If True (default), disable StrictHostKeyChecking.
    """
    if not _BUBBLE_NAME_RE.match(bubble_name):
        raise ValueError(f"Invalid bubble name for SSH config: {bubble_name!r}")
    SSH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    incus_cmd = f'incus exec {bubble_name} -- su - {user} -c "nc localhost 22"'

    if remote_host is not None:
        ssh_parts = ["ssh"]
        if remote_host.ssh_options:
            ssh_parts += remote_host.ssh_options
        if remote_host.port != 22:
            ssh_parts += ["-p", str(remote_host.port)]
        ssh_parts.append(remote_host.ssh_destination)
        # incus_cmd must be passed as a single argument so inner quotes
        # (e.g. "nc localhost 22") survive the local shell → SSH → remote
        # shell chain.
        proxy_cmd = " ".join(shlex.quote(p) for p in ssh_parts) + " " + shlex.quote(incus_cmd)
    else:
        proxy_cmd = incus_cmd

    lines = [
        f"Host bubble-{bubble_name}",
        f"  User {user}",
        f"  ProxyCommand {proxy_cmd}",
    ]
    if host_key_trust:
        lines.append("  StrictHostKeyChecking no")
        lines.append("  UserKnownHostsFile /dev/null")
    lines.append("  LogLevel ERROR")

    entry = "\n" + "\n".join(lines) + "\n"
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
    elif editor in ("emacs", "neovim"):
        editor_cmd = "emacs" if editor == "emacs" else "nvim"
        ssh_cmd = [
            "ssh",
            f"bubble-{bubble_name}",
            "-t",
            f"cd {shlex.quote(remote_path)} && {editor_cmd} .",
        ]
        subprocess.run(ssh_cmd)
    elif editor == "shell":
        ssh_cmd = ["ssh", f"bubble-{bubble_name}"]
        if command:
            ssh_cmd += command
        subprocess.run(ssh_cmd)


def open_editor_native(editor: str, local_path: str, command: list[str] | None = None):
    """Open the specified editor for a native (non-containerized) workspace.

    Opens VSCode directly on the local path, or spawns a shell in that directory.
    """
    if editor == "vscode":
        try:
            subprocess.run(
                ["code", "--disable-workspace-trust", "--folder-uri", f"file://{local_path}"],
                check=True,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f"VSCode CLI not found or failed. Open manually: {local_path}")
    elif editor == "shell":
        if command:
            subprocess.run(command, cwd=local_path)
        else:
            subprocess.run(
                ["bash", "-c", f"cd {shlex.quote(local_path)} && exec $SHELL"],
            )


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
        subprocess.run(
            ["code", "--disable-workspace-trust", flag, uri],
            check=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        if workspace_file:
            # Fall back to opening the folder if --file-uri fails
            folder_uri = f"vscode-remote://ssh-remote+{host}{remote_path}"
            try:
                subprocess.run(
                    ["code", "--disable-workspace-trust", "--folder-uri", folder_uri],
                    check=True,
                    stderr=subprocess.DEVNULL,
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass
    except FileNotFoundError:
        print(f"VSCode CLI not found. Connect manually: Remote SSH → {host}")
        print(f"Or run: code {flag} {uri}")
