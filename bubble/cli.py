"""CLI entry point for bubble."""

import json
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click

from . import __version__
from .config import ensure_dirs, load_config, repo_short_name
from .git_store import bare_repo_path, ensure_repo, fetch_ref, github_url, update_all_repos
from .hooks import select_hook
from .lifecycle import _load_registry, register_bubble, unregister_bubble
from .naming import deduplicate_name, generate_name
from .repo_registry import RepoRegistry
from .runtime.base import ContainerRuntime
from .runtime.incus import IncusRuntime
from .target import TargetParseError, parse_target
from .vscode import (
    add_ssh_config,
    ensure_vscode_extensions,
    open_vscode,
    remove_ssh_config,
)


def _is_command_available(cmd: str) -> bool:
    """Check if a command is available on PATH."""
    try:
        subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_nixos() -> bool:
    """Check if we're running on NixOS."""
    return Path("/etc/nixos").is_dir() or Path("/etc/NIXOS").exists()


def _is_debian_based() -> bool:
    """Check if we're on a Debian/Ubuntu system with apt."""
    return _is_command_available("apt-get") and Path("/etc/os-release").exists()


def _install_incus_debian():
    """Install Incus on Debian/Ubuntu via the Zabbly repository."""
    click.echo("Installing Incus from the Zabbly repository...")

    # Add GPG key
    subprocess.run(
        ["sudo", "mkdir", "-p", "/etc/apt/keyrings/"],
        check=True,
    )
    key_data = subprocess.run(
        ["curl", "-fsSL", "https://pkgs.zabbly.com/key.asc"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["sudo", "tee", "/etc/apt/keyrings/zabbly.asc"],
        input=key_data.stdout, capture_output=True, check=True,
    )

    # Determine codename and architecture
    os_release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            os_release[k] = v.strip('"')
    codename = os_release.get("VERSION_CODENAME", "jammy")
    arch = subprocess.run(
        ["dpkg", "--print-architecture"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Add repository
    sources_content = (
        f"Enabled: yes\n"
        f"Types: deb\n"
        f"URIs: https://pkgs.zabbly.com/incus/stable\n"
        f"Suites: {codename}\n"
        f"Components: main\n"
        f"Architectures: {arch}\n"
        f"Signed-By: /etc/apt/keyrings/zabbly.asc\n"
    )
    subprocess.run(
        ["sudo", "tee", "/etc/apt/sources.list.d/zabbly-incus-stable.sources"],
        input=sources_content.encode(), capture_output=True, check=True,
    )

    # Install
    subprocess.run(["sudo", "apt-get", "update"], check=True)
    subprocess.run(["sudo", "apt-get", "install", "-y", "incus"], check=True)

    # Initialize with minimal defaults
    click.echo("Initializing Incus...")
    subprocess.run(["sudo", "incus", "admin", "init", "--minimal"], check=True)

    # Add current user to incus-admin group
    import getpass

    username = getpass.getuser()
    click.echo(f"Adding {username} to the incus-admin group...")
    subprocess.run(["sudo", "usermod", "-aG", "incus-admin", username], check=True)

    click.echo()
    click.echo("Incus installed successfully.")
    click.echo("NOTE: You need to log out and back in for group membership to take effect.")
    click.echo("  Or run: newgrp incus-admin")
    click.echo("  Then re-run your bubble command.")
    sys.exit(0)


def _ensure_dependencies():
    """Check for required dependencies and offer to install them interactively."""
    system = platform.system()

    if system == "Darwin":
        # Check Homebrew
        if not _is_command_available("brew"):
            click.echo("Homebrew is required but not installed.")
            click.echo("  Install it with:")
            click.echo('  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
            sys.exit(1)

        # Check Colima and Incus
        missing = []
        if not _is_command_available("colima"):
            missing.append("colima")
        if not _is_command_available("incus"):
            missing.append("incus")

        if missing:
            names = " and ".join(missing)
            cmd = "brew install " + " ".join(missing)
            click.echo(f"{names} {'is' if len(missing) == 1 else 'are'} required but not installed.")
            if click.confirm(f"  Install via Homebrew? ({cmd})", default=True):
                subprocess.run(["brew", "install"] + missing, check=True)
            else:
                click.echo(f"  To install manually: {cmd}")
                sys.exit(1)

    elif system == "Linux":
        if not _is_command_available("incus"):
            if _is_nixos():
                click.echo("Incus is required but not installed.")
                click.echo("  Add to your NixOS configuration:")
                click.echo()
                click.echo("    virtualisation.incus.enable = true;")
                click.echo("    networking.nftables.enable = true;")
                click.echo('    users.users.YOUR_USER.extraGroups = ["incus-admin"];')
                click.echo()
                click.echo("  Then run: sudo nixos-rebuild switch")
                sys.exit(1)
            elif _is_debian_based():
                click.echo("Incus is required but not installed.")
                if click.confirm(
                    "  Install via the Zabbly repository? (requires sudo)", default=True
                ):
                    _install_incus_debian()
                else:
                    click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                    sys.exit(1)
            else:
                click.echo("Incus is required but not installed.")
                click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                sys.exit(1)


def get_runtime(config: dict, ensure_ready: bool = True) -> ContainerRuntime:
    """Get the configured container runtime. Ensures platform is ready by default."""
    if ensure_ready:
        _ensure_dependencies()
        if platform.system() == "Darwin":
            from .runtime.colima import ensure_colima

            rt = config["runtime"]
            ensure_colima(
                cpu=rt["colima_cpu"],
                memory=rt["colima_memory"],
                disk=rt.get("colima_disk", 60),
                vm_type=rt.get("colima_vm_type", "vz"),
            )
    backend = config["runtime"]["backend"]
    if backend == "incus":
        return IncusRuntime()
    raise ValueError(f"Unknown runtime backend: {backend}")


def _find_container(runtime: ContainerRuntime, name: str):
    """Find a container by name. Returns ContainerInfo or exits."""
    for c in runtime.list_containers():
        if c.name == name:
            return c
    click.echo(f"Bubble '{name}' not found.", err=True)
    sys.exit(1)


def _ensure_running(runtime: ContainerRuntime, name: str):
    """Ensure a container is running (unpause/start if needed)."""
    info = _find_container(runtime, name)
    if info.state == "frozen":
        click.echo(f"Unpausing '{name}'...")
        runtime.unfreeze(name)
    elif info.state == "stopped":
        click.echo(f"Starting '{name}'...")
        runtime.start(name)
    return info


def _setup_ssh(runtime: ContainerRuntime, name: str):
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
            subprocess.run(
                ["incus", "file", "push", tmp_keys, f"{name}/home/user/.ssh/authorized_keys"],
                check=True,
                capture_output=True,
            )
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

    add_ssh_config(name)


def _apply_network(runtime: ContainerRuntime, name: str, config: dict,
                    extra_domains: list[str] | None = None):
    """Apply network allowlist to a container if configured."""
    domains = list(config.get("network", {}).get("allowlist", []))
    if extra_domains:
        for d in extra_domains:
            if d not in domains:
                domains.append(d)
    if domains:
        try:
            from .network import apply_allowlist

            apply_allowlist(runtime, name, domains)
            click.echo("  Network allowlist applied.")
        except Exception as e:
            click.echo(f"  Warning: could not apply network allowlist: {e}")


def _detect_project_dir(runtime: ContainerRuntime, name: str) -> str:
    """Detect the project directory inside a container."""
    try:
        return (
            runtime.exec(name, ["bash", "-c", "ls -d /home/user/*/ 2>/dev/null | head -1"])
            .strip()
            .rstrip("/")
        )
    except Exception:
        return "/home/user"


def _maybe_install_automation():
    """Install automation jobs on first use if not already present."""
    from .automation import install_automation, is_automation_installed

    try:
        status = is_automation_installed()
        if status and not any(status.values()):
            click.echo("Installing automation (hourly git update, weekly image refresh)...")
            click.echo("  To remove later: bubble automation remove")
            installed = install_automation()
            for item in installed:
                click.echo(f"  {item}")
    except Exception:
        pass


def _find_existing_container(runtime: ContainerRuntime, target_str: str,
                             generated_name: str | None = None,
                             org_repo: str | None = None,
                             kind: str | None = None,
                             ref: str | None = None) -> str | None:
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
        registry = _load_registry()
        for bname, binfo in registry.get("bubbles", {}).items():
            if binfo.get("state") != "active":
                continue
            if binfo.get("org_repo") != org_repo:
                continue
            if bname not in containers:
                continue
            if kind == "pr" and str(binfo.get("pr", "")) == ref:
                return bname
            if kind == "branch" and binfo.get("branch") == ref:
                return bname

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class BubbleGroup(click.Group):
    """Custom group that routes unknown first args to the implicit 'open' command."""

    def parse_args(self, ctx, args):
        """If the first arg isn't a known command, treat it as a target for 'open'."""
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["open"] + args
        return super().parse_args(ctx, args)


@click.group(cls=BubbleGroup)
@click.version_option(__version__)
def main():
    """bubble: Containerized development environments."""


# ---------------------------------------------------------------------------
# open (the primary command, invoked implicitly)
# ---------------------------------------------------------------------------


@main.command("open")
@click.argument("target")
@click.option("--ssh", is_flag=True, help="Drop into SSH session instead of VSCode")
@click.option("--no-interactive", is_flag=True, help="Just create, don't attach")
@click.option("--network/--no-network", default=True, help="Apply network allowlist")
@click.option("--name", "custom_name", type=str, help="Custom container name")
def open_cmd(target, ssh, no_interactive, network, custom_name):
    """Open a bubble for a GitHub target (URL, org/repo, or shorthand)."""
    config = load_config()
    runtime = get_runtime(config)

    # Step 1: Check if target matches an existing container name
    existing = _find_existing_container(runtime, target)
    if existing:
        _reattach(runtime, existing, ssh, no_interactive)
        return

    # Step 2: Parse target
    registry = RepoRegistry()
    try:
        t = parse_target(target, registry)
    except TargetParseError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    # Step 3: Register repo in RepoRegistry
    registry.register(t.owner, t.repo)

    # Step 4: Generate name and check for existing container
    if custom_name:
        name = custom_name
    elif t.kind == "pr":
        name = generate_name(t.short_name, "pr", t.ref)
    elif t.kind == "branch":
        name = generate_name(t.short_name, "branch", t.ref)
    elif t.kind == "commit":
        name = generate_name(t.short_name, "commit", t.ref[:12])
    else:
        name = generate_name(t.short_name, "main", "")

    existing = _find_existing_container(
        runtime, target, generated_name=name,
        org_repo=t.org_repo, kind=t.kind, ref=t.ref,
    )
    if existing:
        _reattach(runtime, existing, ssh, no_interactive)
        return

    # Step 5: Ensure dirs and bare repo
    ensure_dirs()

    bare_path = ensure_repo(t.org_repo)

    # Step 6: Fetch specific ref if needed
    if t.kind == "pr":
        click.echo(f"Fetching PR #{t.ref}...")
        try:
            fetch_ref(t.org_repo, f"refs/pull/{t.ref}/head:refs/pull/{t.ref}/head")
        except Exception:
            # May already be available from a full fetch
            pass

    # Step 7: Hook detection
    if t.kind == "pr":
        hook_ref = f"refs/pull/{t.ref}/head"
    elif t.kind == "branch":
        hook_ref = t.ref
    elif t.kind == "commit":
        hook_ref = t.ref
    else:
        hook_ref = "HEAD"

    hook = select_hook(bare_path, hook_ref)
    if hook:
        click.echo(f"  Detected: {hook.name()}")
        image_name = hook.image_name()
    else:
        image_name = "bubble-base"

    # Step 8: Ensure image exists
    if not runtime.image_exists(image_name):
        click.echo(f"Building {image_name} image (one-time setup, may take a few minutes)...")
        from .images.builder import build_image

        build_image(runtime, image_name)
        click.echo(f"  {image_name} image ready.")

    # Deduplicate name
    existing_names = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing_names)

    click.echo(f"Creating bubble '{name}'...")

    # Step 9: Launch container
    runtime.launch(name, image_name)

    # Wait for container to be ready
    for _ in range(30):
        try:
            runtime.exec(name, ["true"])
            try:
                runtime.exec(name, ["getent", "hosts", "github.com"])
                break
            except Exception:
                time.sleep(1)
        except Exception:
            time.sleep(1)

    # Mount bare repo
    if bare_path.exists():
        runtime.add_disk(
            name, "shared-git", str(bare_path), f"/shared/git/{bare_path.name}", readonly=True
        )

    # Clone repo inside container
    short = repo_short_name(t.org_repo)
    url = github_url(t.org_repo)
    click.echo(f"Cloning {t.org_repo} (using shared objects)...")
    runtime.exec(
        name,
        [
            "su",
            "-",
            "user",
            "-c",
            f"git clone --reference /shared/git/{bare_path.name} {url} /home/user/{short}",
        ],
    )

    # Checkout the appropriate ref
    checkout_branch = ""
    if t.kind == "pr":
        click.echo(f"Checking out PR #{t.ref}...")
        checkout_branch = f"pr-{t.ref}"
        q_branch = shlex.quote(checkout_branch)
        runtime.exec(
            name,
            [
                "su",
                "-",
                "user",
                "-c",
                f"cd /home/user/{short} && git fetch origin pull/{t.ref}/head:{q_branch}"
                f" && git checkout {q_branch}",
            ],
        )
    elif t.kind == "branch":
        click.echo(f"Checking out branch '{t.ref}'...")
        checkout_branch = t.ref
        q_branch = shlex.quote(t.ref)
        runtime.exec(
            name,
            [
                "su",
                "-",
                "user",
                "-c",
                f"cd /home/user/{short} && git checkout {q_branch}",
            ],
        )
    elif t.kind == "commit":
        click.echo(f"Checking out commit {t.ref[:12]}...")
        q_commit = shlex.quote(t.ref)
        runtime.exec(
            name,
            [
                "su",
                "-",
                "user",
                "-c",
                f"cd /home/user/{short} && git checkout {q_commit}",
            ],
        )

    # Step 10: Run hook post_clone
    project_dir = f"/home/user/{short}"
    if hook:
        hook.post_clone(runtime, name, project_dir)

    # Step 11: Ensure hook's VSCode extensions
    if hook:
        ensure_vscode_extensions(hook.vscode_extensions())

    # Step 12: Apply network allowlist (merging hook domains)
    if network:
        extra_domains = hook.network_domains() if hook else None
        _apply_network(runtime, name, config, extra_domains)

    # Set up SSH access
    click.echo("Setting up SSH access...")
    _setup_ssh(runtime, name)

    # Register in lifecycle
    commit = ""
    try:
        commit = runtime.exec(
            name,
            [
                "su",
                "-",
                "user",
                "-c",
                f"cd /home/user/{short} && git rev-parse HEAD",
            ],
        ).strip()
    except Exception:
        pass
    register_bubble(
        name, t.org_repo,
        branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        commit=commit,
        pr=int(t.ref) if t.kind == "pr" else 0,
        base_image=image_name,
    )

    # Install automation on first bubble creation if not already installed
    _maybe_install_automation()

    click.echo(f"Bubble '{name}' created successfully.")
    click.echo(f"  SSH: ssh bubble-{name}")

    # Step 13: Attach
    if not no_interactive:
        if ssh:
            click.echo(f"Connecting via SSH...")
            subprocess.run(["ssh", f"bubble-{name}"])
        else:
            click.echo("Opening VSCode...")
            open_vscode(name, project_dir)


def _reattach(runtime: ContainerRuntime, name: str, ssh: bool, no_interactive: bool):
    """Re-attach to an existing container."""
    _ensure_running(runtime, name)

    if no_interactive:
        click.echo(f"Bubble '{name}' is running.")
        return

    project_dir = _detect_project_dir(runtime, name)

    if ssh:
        click.echo(f"Connecting to '{name}' via SSH...")
        subprocess.run(["ssh", f"bubble-{name}"])
    else:
        click.echo(f"Opening VSCode for '{name}'...")
        open_vscode(name, project_dir)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_bubbles(as_json):
    """List all bubbles."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)

    containers = runtime.list_containers()

    if as_json:
        data = [{"name": c.name, "state": c.state, "ipv4": c.ipv4} for c in containers]
        click.echo(json.dumps(data, indent=2))
        return

    if not containers:
        click.echo("No bubbles. Create one with: bubble owner/repo")
        return

    click.echo(f"{'NAME':<30} {'STATE':<10} {'IPv4':<16}")
    click.echo("-" * 56)
    for c in containers:
        click.echo(f"{c.name:<30} {c.state:<10} {c.ipv4 or '-':<16}")


# ---------------------------------------------------------------------------
# pause / destroy
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def pause(name):
    """Pause (freeze) a bubble."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    runtime.freeze(name)
    click.echo(f"Bubble '{name}' paused.")


@main.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Force destroy even if running")
def destroy(name, force):
    """Destroy a bubble permanently."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)

    if not force:
        click.confirm(f"Permanently destroy bubble '{name}'?", abort=True)

    runtime.delete(name, force=True)
    remove_ssh_config(name)
    unregister_bubble(name)
    click.echo(f"Bubble '{name}' destroyed.")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


@main.group("images")
def images_group():
    """Manage base images."""


@images_group.command("list")
def images_list():
    """List available base images."""
    try:
        output = subprocess.run(
            ["incus", "image", "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        )
        images = json.loads(output.stdout)
        if not images:
            click.echo("No images. Run: bubble images build bubble-base")
            return
        click.echo(f"{'ALIAS':<25} {'SIZE':<12} {'CREATED':<20}")
        click.echo("-" * 57)
        for img in images:
            aliases = ", ".join(a["name"] for a in img.get("aliases", []))
            size_mb = img.get("size", 0) / (1024 * 1024)
            created = img.get("created_at", "")[:19]
            click.echo(f"{aliases:<25} {size_mb:>8.1f} MB  {created:<20}")
    except Exception as e:
        click.echo(f"Error listing images: {e}", err=True)


@images_group.command("build")
@click.argument("image_name", default="bubble-base")
def images_build(image_name):
    """Build a base image (bubble-base, bubble-lean)."""
    config = load_config()
    runtime = get_runtime(config)

    from .images.builder import build_image

    try:
        build_image(runtime, image_name)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


@main.group("git")
def git_group():
    """Manage shared git object store."""


@git_group.command("update")
def git_update():
    """Update all shared bare repos."""
    update_all_repos()
    click.echo("Git store updated.")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


@main.group("network")
def network_group():
    """Manage network allowlisting."""


@network_group.command("apply")
@click.argument("name")
def network_apply(name):
    """Apply network allowlist to a bubble."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    _ensure_running(runtime, name)

    domains = config.get("network", {}).get("allowlist", [])
    if not domains:
        click.echo("No domains in allowlist. Edit ~/.bubble/config.toml", err=True)
        sys.exit(1)

    from .network import apply_allowlist

    apply_allowlist(runtime, name, domains)
    click.echo(f"Network allowlist applied to '{name}' ({len(domains)} domains).")


@network_group.command("remove")
@click.argument("name")
def network_remove(name):
    """Remove network restrictions from a bubble."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    _ensure_running(runtime, name)

    from .network import remove_allowlist

    remove_allowlist(runtime, name)
    click.echo(f"Network restrictions removed from '{name}'.")


# ---------------------------------------------------------------------------
# automation
# ---------------------------------------------------------------------------


@main.group("automation")
def automation_group():
    """Manage automated tasks (git update, image refresh)."""


@automation_group.command("install")
def automation_install():
    """Install automation jobs (launchd on macOS, systemd on Linux)."""
    from .automation import install_automation

    installed = install_automation()
    if installed:
        for item in installed:
            click.echo(f"  Installed: {item}")
        click.echo("Automation installed.")
    else:
        click.echo("No automation installed (unsupported platform?).", err=True)


@automation_group.command("remove")
def automation_remove():
    """Remove all automation jobs."""
    from .automation import remove_automation

    removed = remove_automation()
    if removed:
        for item in removed:
            click.echo(f"  Removed: {item}")
        click.echo("Automation removed.")
    else:
        click.echo("No automation jobs found to remove.")


@automation_group.command("status")
def automation_status():
    """Show automation status."""
    from .automation import is_automation_installed

    status = is_automation_installed()
    if not status:
        click.echo("Automation not supported on this platform.")
        return
    for job, installed in status.items():
        state = "installed" if installed else "not installed"
        click.echo(f"  {job}: {state}")


if __name__ == "__main__":
    main()
