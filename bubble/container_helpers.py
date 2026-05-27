"""Shared container helper functions: find, ensure running, SSH, git config, network."""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from .lifecycle import load_registry
from .output import detail, step
from .runtime.base import ContainerRuntime
from .security import filter_github_domains
from .vscode import add_ssh_config


def collect_authorized_keys(config: dict | None = None) -> list[str]:
    """Collect public key contents to inject as authorized_keys in containers.

    Resolution order (first match wins):
      1. ``BUBBLE_AUTHORIZED_KEYS`` env var (colon-separated paths).
      2. ``[ssh] authorized_keys`` config — string path or list of paths.
      3. Default: ``~/.ssh/id_ed25519.pub`` if present.
      4. Fallback (only when no ed25519 key exists): ``~/.ssh/id_rsa.pub``
         and ``~/.ssh/id_ecdsa.pub``.

    Paths support ``~`` expansion. With (1) or (2), every listed path must
    exist; missing files raise ``ClickException``. An explicit empty list
    means "no keys" — SSH will still be running but nothing will be
    authorized.
    """
    explicit_paths: list[str] | None = None

    env_value = os.environ.get("BUBBLE_AUTHORIZED_KEYS")
    if env_value is not None:
        explicit_paths = [p for p in env_value.split(":") if p]
    elif config is not None:
        cfg_value = config.get("ssh", {}).get("authorized_keys")
        if isinstance(cfg_value, str):
            explicit_paths = [cfg_value] if cfg_value else []
        elif isinstance(cfg_value, list):
            explicit_paths = [str(p) for p in cfg_value]
        elif cfg_value is not None:
            raise click.ClickException(
                f"[ssh] authorized_keys must be a string or list of strings, "
                f"got {type(cfg_value).__name__}"
            )

    if explicit_paths is not None:
        keys: list[str] = []
        for path_str in explicit_paths:
            path = Path(path_str).expanduser()
            if not path.exists():
                raise click.ClickException(f"SSH key file not found: {path_str}")
            content = path.read_text().strip()
            if content:
                keys.append(content)
        return keys

    ssh_dir = Path.home() / ".ssh"
    ed25519 = ssh_dir / "id_ed25519.pub"
    if ed25519.exists():
        content = ed25519.read_text().strip()
        return [content] if content else []

    keys = []
    for fallback in ("id_rsa.pub", "id_ecdsa.pub"):
        path = ssh_dir / fallback
        if path.exists():
            content = path.read_text().strip()
            if content:
                keys.append(content)
    return keys


def find_container(runtime: ContainerRuntime, name: str):
    """Find a container by name. Returns ContainerInfo or exits."""
    for c in runtime.list_containers():
        if c.name == name:
            return c
    click.echo(f"Bubble '{name}' not found. Run 'bubble list' to see your bubbles.", err=True)
    sys.exit(1)


def ensure_running(runtime: ContainerRuntime, name: str):
    """Ensure a container is running, restoring iptables rules on stop/start.

    ``incus stop`` destroys the container's network namespace, which drops
    the iptables allowlist installed at provision time. Without an explicit
    replay the container would silently come back up with default-ACCEPT
    egress (issue #285). Freeze/unfreeze (incus pause/start) keeps the
    namespace intact, so paused bubbles don't need replay.

    On replay failure the container is stopped again so the next attempt
    starts from a clean ``stopped`` state — failing closed rather than
    leaving an unprotected container running.
    """
    info = find_container(runtime, name)
    prior_state = info.state
    if prior_state == "frozen":
        step(f"Unpausing '{name}'...")
        runtime.unfreeze(name)
    elif prior_state == "stopped":
        step(f"Starting '{name}'...")
        runtime.start(name)
        try:
            reapply_network_after_restart(runtime, name)
        except Exception:
            try:
                runtime.stop(name)
            except Exception:
                pass
            raise
    return info


def reapply_network_after_restart(runtime: ContainerRuntime, name: str):
    """Re-apply the network allowlist after an ``incus stop``/``start`` cycle.

    Looks up the bubble's network state from the registry: ``network_enabled``
    decides whether to re-apply at all (so ``--no-network`` bubbles aren't
    suddenly locked down), and ``extra_domains`` restores the hook-contributed
    allowlist that the container was originally provisioned with.

    Legacy entries that predate the registry fields are handled
    conservatively: ``network_enabled`` defaults to ``True`` (the historical
    default of the ``--network`` flag) and ``extra_domains`` is recovered by
    re-detecting the language hook against the bare repo.
    """
    from .config import load_config
    from .lifecycle import get_bubble_info

    info = get_bubble_info(name) or {}
    if not info.get("network_enabled", True):
        return

    extra_domains = info.get("extra_domains")
    if extra_domains is None:
        extra_domains = recover_extra_domains(info) or []

    config = load_config()
    apply_network(runtime, name, config, extra_domains=list(extra_domains))


def recover_extra_domains(info: dict) -> list[str] | None:
    """Best-effort recovery of hook domains for legacy registry entries.

    Returns ``None`` when recovery isn't possible (no org_repo, no bare repo,
    or no matching hook). Prefers ``commit`` over ``branch`` so that long-lived
    bubbles whose branch tip has advanced upstream still see the same hook
    output that the bubble was originally provisioned with.
    """
    org_repo = info.get("org_repo")
    if not org_repo:
        return None
    repo_short = org_repo.split("/")[-1] if "/" in org_repo else org_repo
    from .config import DATA_DIR
    from .hooks import select_hook

    bare_repo = DATA_DIR / "git" / f"{repo_short}.git"
    if not bare_repo.exists():
        return None
    ref = info.get("commit") or info.get("branch") or "HEAD"
    try:
        hook = select_hook(bare_repo, ref)
    except Exception:
        return None
    return list(hook.network_domains()) if hook else []


def setup_ssh(
    runtime: ContainerRuntime,
    name: str,
    host_key_trust: bool = True,
    config: dict | None = None,
):
    """Start SSH and inject host public keys into a container."""
    runtime.exec(name, ["bash", "-c", "service ssh start || /usr/sbin/sshd"])

    pub_keys = collect_authorized_keys(config)
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
    runtime: ContainerRuntime,
    name: str,
    config: dict,
    extra_domains: list[str] | None = None,
    keep_github_domains: bool = False,
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
    # Direct GitHub network access is only allowed when the github level
    # is "direct" (raw token injection). For proxy-mediated access or no
    # auth, iptables blocks direct GitHub traffic — all GitHub communication
    # is forced through the auth proxy on loopback.
    # Exception: when auth setup is deferred (remote orchestration), keep
    # GitHub domains temporarily so the initial clone can proceed directly.
    # Never keep them when github=off (no GitHub access at all).
    from .security import get_github_level

    gh_level = get_github_level(config)
    if gh_level != "direct" and not (keep_github_domains and gh_level != "off"):
        domains = filter_github_domains(domains)
    # If the bridge-listener auth proxy is running, punch a hole for the
    # bridge IP:port so the container can reach it directly. Bubbles
    # using the legacy proxy-device flow don't need this (the proxy
    # device delivers traffic on the container's own loopback).
    auth_endpoint = _resolve_auth_proxy_endpoint_for_allowlist(gh_level)
    if domains or auth_endpoint:
        try:
            from .network import apply_allowlist

            apply_allowlist(runtime, name, domains, auth_proxy_endpoint=auth_endpoint)
            detail("Network allowlist applied.")
        except (RuntimeError, OSError, ValueError) as e:
            raise click.ClickException(f"Failed to apply network allowlist: {e}")


def _resolve_auth_proxy_endpoint_for_allowlist(gh_level: str) -> tuple[str, int] | None:
    """Return the bridge auth-proxy endpoint if the bridge flow is active.

    Returns None when github auth is disabled, when injecting the raw
    token (no proxy needed), or when the daemon hasn't written the v2
    endpoint file (legacy flow). On macOS we likewise return None: the
    proxy listens on the Colima bridge IP, but the container reaches
    it via the legacy proxy-device path, not a host iptables rule.
    """
    import json
    import platform

    if gh_level in ("off", "direct"):
        return None
    if platform.system() != "Linux":
        return None
    from .auth_proxy import AUTH_PROXY_ENDPOINT_FILE

    if not AUTH_PROXY_ENDPOINT_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_PROXY_ENDPOINT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    tcp = data.get("tcp") or {}
    host = tcp.get("host")
    port = tcp.get("port")
    if not host or not isinstance(port, int):
        return None
    # Don't punch a hole for loopback — that's the legacy bind fallback
    # and the proxy device handles delivery internally.
    if host == "127.0.0.1":
        return None
    return (host, port)


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
