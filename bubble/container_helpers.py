"""Shared container helper functions: find, ensure running, SSH, git config, network."""

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from .lifecycle import load_registry, prune_stale_entries
from .output import detail, step
from .runtime.base import ContainerRuntime
from .security import filter_github_domains, is_enabled
from .vscode import add_ssh_config


def find_container(runtime: ContainerRuntime, name: str):
    """Find a container by name. Returns ContainerInfo or exits."""
    for c in runtime.list_containers():
        if c.name == name:
            return c
    click.echo(f"Bubble '{name}' not found. Run 'bubble list' to see your bubbles.", err=True)
    sys.exit(1)


def ensure_running(runtime: ContainerRuntime, name: str):
    """Ensure a container is running (unpause/start if needed)."""
    info = find_container(runtime, name)
    if info.state == "frozen":
        step(f"Unpausing '{name}'...")
        runtime.unfreeze(name)
    elif info.state == "stopped":
        step(f"Starting '{name}'...")
        runtime.start(name)
    return info


def setup_ssh(runtime: ContainerRuntime, name: str, host_key_trust: bool = True):
    """Start SSH and inject host public keys into a container."""
    runtime.exec(name, ["bash", "-c", "service ssh start || /usr/sbin/sshd"])

    ssh_dir = Path.home() / ".ssh"
    pub_keys = []
    for key_file in ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]:
        key_path = ssh_dir / key_file
        if key_path.exists():
            pub_keys.append(key_path.read_text().strip())
    if pub_keys:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".keys", delete=False) as f:
            f.write("\n".join(pub_keys) + "\n")
            tmp_keys = f.name
        try:
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "user",
                    "-c",
                    "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                ],
            )
            runtime.push_file(name, tmp_keys, "/home/user/.ssh/authorized_keys")
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    "chown user:user /home/user/.ssh/authorized_keys"
                    " && chmod 600 /home/user/.ssh/authorized_keys",
                ],
            )
        finally:
            Path(tmp_keys).unlink(missing_ok=True)

    add_ssh_config(name, host_key_trust=host_key_trust)


def get_host_git_identity() -> tuple[str, str]:
    """Read git user.name and user.email from host's git config."""
    name = email = ""
    for key in ("user.name", "user.email"):
        try:
            val = subprocess.run(
                ["git", "config", key],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            val = ""
        if key == "user.name":
            name = val
        else:
            email = val
    return name, email


def setup_git_config(runtime: ContainerRuntime, name: str, git_name: str, git_email: str):
    """Inject git identity into a container."""
    cmds = []
    if git_name:
        cmds.append(f"git config --global user.name {shlex.quote(git_name)}")
    if git_email:
        cmds.append(f"git config --global user.email {shlex.quote(git_email)}")
    if cmds:
        runtime.exec(name, ["su", "-", "user", "-c", " && ".join(cmds)])


def apply_network(
    runtime: ContainerRuntime, name: str, config: dict, extra_domains: list[str] | None = None
):
    """Apply network allowlist to a container if configured."""
    domains = list(config.get("network", {}).get("allowlist", []))
    if extra_domains:
        for d in extra_domains:
            if d not in domains:
                domains.append(d)
    # Include runtime domains for enabled tools (e.g. API endpoints)
    from .tools import resolve_tools, tool_runtime_domains

    for d in tool_runtime_domains(resolve_tools(config)):
        if d not in domains:
            domains.append(d)
    # Strip ALL GitHub domains after merging all sources (base, hooks, tools)
    if not is_enabled(config, "network_github"):
        domains = filter_github_domains(domains)
    if domains:
        try:
            from .network import apply_allowlist

            apply_allowlist(runtime, name, domains)
            detail("Network allowlist applied.")
        except (RuntimeError, OSError, ValueError) as e:
            raise click.ClickException(f"Failed to apply network allowlist: {e}")


def detect_project_dir(runtime: ContainerRuntime, name: str) -> str:
    """Detect the project directory inside a container.

    Looks up the registry first; falls back to the ls heuristic for
    pre-existing bubbles that don't have project_dir stored.
    """
    from .lifecycle import get_bubble_info

    info = get_bubble_info(name)
    if info and info.get("project_dir"):
        return info["project_dir"]

    # Fallback for legacy bubbles without project_dir in registry
    try:
        result = (
            runtime.exec(name, ["bash", "-c", "ls -d /home/user/*/ 2>/dev/null | head -1"])
            .strip()
            .rstrip("/")
        )
        return result or "/home/user"
    except Exception:
        return "/home/user"


def maybe_install_automation():
    """Install automation jobs on first use if not already present."""
    from .automation import install_automation, is_automation_installed

    try:
        status = is_automation_installed()
        if status and not any(status.values()):
            step("Installing automation (hourly git update, weekly image refresh)...")
            detail("To remove later: bubble automation remove")
            installed = install_automation()
            for item in installed:
                detail(item)
    except (OSError, subprocess.CalledProcessError):
        pass  # Best-effort; failures surface via `bubble doctor`


def maybe_install_skill():
    """Auto-install the Claude Code skill on first bubble creation."""
    from .skill import claude_code_detected, install_skill, is_installed

    try:
        if not claude_code_detected() or is_installed():
            return
        msg = install_skill()
        detail(msg)
        detail("To manage later: bubble skill status")
    except (OSError, subprocess.CalledProcessError, ImportError):
        pass  # Best-effort; failures surface via `bubble doctor`


def find_existing_container(
    runtime: ContainerRuntime,
    target_str: str,
    generated_name: str | None = None,
    org_repo: str | None = None,
    kind: str | None = None,
    ref: str | None = None,
) -> str | None:
    """Find an existing container matching the target. Returns name or None."""
    containers = {c.name for c in runtime.list_containers()}
    prune_stale_entries(containers)

    # Check if raw target string matches a container name
    if target_str in containers:
        return target_str

    # Check by generated name
    if generated_name and generated_name in containers:
        return generated_name

    # Check registry for same org_repo + PR/branch
    if org_repo and kind and ref:
        registry = load_registry()
        for bname, binfo in registry.get("bubbles", {}).items():
            if binfo.get("org_repo") != org_repo:
                continue
            if bname not in containers:
                continue
            if kind == "pr" and str(binfo.get("pr", "")) == ref:
                return bname
            if kind == "branch" and binfo.get("branch") == ref:
                return bname

    return None
