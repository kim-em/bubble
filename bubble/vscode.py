"""VSCode Remote SSH integration."""

import contextlib
import fcntl
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .config import DATA_DIR

# Valid bubble name pattern (alphanumeric + hyphens, starts with letter)
_BUBBLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

SSH_CONFIG_DIR = Path.home() / ".ssh" / "config.d"
SSH_CONFIG_FILE = SSH_CONFIG_DIR / "bubble"
SSH_MAIN_CONFIG = Path.home() / ".ssh" / "config"
_SSH_CONFIG_LOCK_FILE = DATA_DIR / "ssh-config.lock"


@contextlib.contextmanager
def _ssh_config_lock():
    """Serialize all SSH-config writes (add/remove + Include directive)."""
    _SSH_CONFIG_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_SSH_CONFIG_LOCK_FILE, "w") as fd:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace *path* with *content*, preserving symlinks and mode.

    If *path* is a symlink, writes to (and replaces) the symlink target rather
    than clobbering the symlink itself — important for users whose
    ~/.ssh/config is symlinked from a dotfiles repo.
    """
    real = path.resolve() if path.is_symlink() else path
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = real.stat().st_mode & 0o777 if real.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=real.name + ".", dir=str(real.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


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
    with _ssh_config_lock():
        SSH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        existing = SSH_CONFIG_FILE.read_text() if SSH_CONFIG_FILE.exists() else ""
        _atomic_write_text(SSH_CONFIG_FILE, existing + entry)
        _ensure_include_directive_locked()


def remove_ssh_config(bubble_name: str):
    """Remove an SSH config entry for a bubble."""
    with _ssh_config_lock():
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

        new_content = "\n".join(result) + "\n" if result else ""
        _atomic_write_text(SSH_CONFIG_FILE, new_content)


def _ensure_include_directive_locked():
    """Ensure ~/.ssh/config includes our config.d directory.

    Caller must hold _ssh_config_lock().
    """
    ssh_config = SSH_MAIN_CONFIG
    include_line = f"Include {SSH_CONFIG_DIR}/*"

    if ssh_config.exists() or ssh_config.is_symlink():
        # is_symlink() check covers a symlink whose target is missing — we
        # still want to update the linked file rather than create a sibling.
        try:
            content = ssh_config.read_text()
        except FileNotFoundError:
            content = ""
        if include_line in content:
            return
        # Prepend the include (must be at top of ssh config)
        _atomic_write_text(ssh_config, include_line + "\n\n" + content)
    else:
        ssh_config.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(ssh_config, include_line + "\n")


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
        # Check for build marker file (written by hooks like LeanHook.post_clone)
        # and start the build in background before launching the editor.
        marker_check = (
            "if [ -f ~/.bubble-fetch-cache ]; then "
            "_cmd=$(cat ~/.bubble-fetch-cache); rm -f ~/.bubble-fetch-cache; "
            'if [ -n "$_cmd" ]; then '
            'nohup bash -c "$_cmd" > ~/build.log 2>&1 & '
            "echo 'Build started in background (tail -f ~/build.log to monitor)'; "
            "fi; fi; "
        )
        ssh_cmd = [
            "ssh",
            f"bubble-{bubble_name}",
            "-t",
            f"{marker_check}cd {shlex.quote(remote_path)} && {editor_cmd} .",
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
