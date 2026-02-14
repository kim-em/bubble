"""CLI entry point for bubble."""

import json
import platform
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from . import __version__
from .config import DATA_DIR, ensure_dirs, load_config, repo_short_name, save_config
from .git_store import bare_repo_path, ensure_repo, fetch_ref, github_url, update_all_repos
from .hooks import select_hook
from .lifecycle import load_registry, register_bubble, unregister_bubble
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
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["sudo", "tee", "/etc/apt/keyrings/zabbly.asc"],
        input=key_data.stdout,
        capture_output=True,
        check=True,
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
        capture_output=True,
        text=True,
        check=True,
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
        input=sources_content.encode(),
        capture_output=True,
        check=True,
    )

    # Install
    subprocess.run(["sudo", "apt-get", "update"], check=True)
    subprocess.run(["sudo", "apt-get", "install", "-y", "incus"], check=True)

    _post_install_incus()


def _install_incus_snap():
    """Install Incus via snap."""
    click.echo("Installing Incus via snap...")
    subprocess.run(["sudo", "snap", "install", "incus", "--channel=latest/stable"], check=True)
    _post_install_incus()


def _post_install_incus():
    """Common post-install steps for Incus on Linux."""
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
            click.echo(
                '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            )
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
            click.echo(
                f"{names} {'is' if len(missing) == 1 else 'are'} required but not installed."
            )
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
            else:
                click.echo("Incus is required but not installed.")
                has_snap = _is_command_available("snap")
                has_apt = _is_debian_based()

                if has_apt and has_snap:
                    choice = click.prompt(
                        "  Install via [1] Zabbly apt repository or [2] snap?",
                        type=click.Choice(["1", "2"]),
                        default="1",
                    )
                    if choice == "1":
                        _install_incus_debian()
                    else:
                        _install_incus_snap()
                elif has_apt:
                    if click.confirm(
                        "  Install via the Zabbly repository? (requires sudo)", default=True
                    ):
                        _install_incus_debian()
                    else:
                        click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                        sys.exit(1)
                elif has_snap:
                    if click.confirm("  Install via snap? (requires sudo)", default=True):
                        _install_incus_snap()
                    else:
                        click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                        sys.exit(1)
                else:
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

    add_ssh_config(name)


def _apply_network(
    runtime: ContainerRuntime, name: str, config: dict, extra_domains: list[str] | None = None
):
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


def _find_existing_container(
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


def _generate_bubble_name(t, custom_name: str | None) -> str:
    """Generate a container name from a parsed target."""
    if custom_name:
        return custom_name
    if t.kind == "pr":
        return generate_name(t.short_name, "pr", t.ref)
    if t.kind == "branch":
        return generate_name(t.short_name, "branch", t.ref)
    if t.kind == "commit":
        return generate_name(t.short_name, "commit", t.ref[:12])
    return generate_name(t.short_name, "main", "")


def _resolve_ref_source(t, no_clone: bool) -> tuple[Path, str]:
    """Resolve the git reference source (bare repo or local .git).

    Returns (ref_path, mount_name).
    """
    if t.local_path:
        try:
            git_dir_result = subprocess.run(
                ["git", "-C", t.local_path, "rev-parse", "--absolute-git-dir"],
                capture_output=True,
                text=True,
                check=True,
            )
            ref_path = Path(git_dir_result.stdout.strip())
        except subprocess.CalledProcessError:
            ref_path = Path(t.local_path) / ".git"
        mount_name = f"{repo_short_name(t.org_repo)}.git"
    else:
        if no_clone:
            bare_path = bare_repo_path(t.org_repo)
            if not bare_path.exists():
                click.echo(f"Repo '{t.org_repo}' is not available in the git store.", err=True)
                sys.exit(1)
        else:
            bare_path = ensure_repo(t.org_repo)
        ref_path = bare_path
        mount_name = ref_path.name

        if t.kind == "pr":
            click.echo(f"Fetching PR #{t.ref}...")
            try:
                fetch_ref(t.org_repo, f"refs/pull/{t.ref}/head:refs/pull/{t.ref}/head")
            except Exception:
                pass  # May already be available from a full fetch

    return ref_path, mount_name


def _detect_and_build_image(runtime, ref_path, t):
    """Detect language hook and ensure image exists. Returns (hook, image_name)."""
    if t.kind == "pr":
        hook_ref = f"refs/pull/{t.ref}/head"
    elif t.kind in ("branch", "commit"):
        hook_ref = t.ref
    else:
        hook_ref = "HEAD"

    hook = select_hook(ref_path, hook_ref)
    if hook:
        click.echo(f"  Detected: {hook.name()}")
        image_name = hook.image_name()
    else:
        image_name = "base"

    if not runtime.image_exists(image_name):
        click.echo(f"Building {image_name} image (one-time setup, may take a few minutes)...")
        from .images.builder import build_image

        build_image(runtime, image_name)
        click.echo(f"  {image_name} image ready.")

    return hook, image_name


def _provision_container(runtime, name, image_name, ref_path, mount_name, config):
    """Launch container, wait for readiness, mount git repo, set up relay."""
    runtime.launch(name, image_name)

    from .images.builder import _wait_for_container

    try:
        _wait_for_container(runtime, name)
    except RuntimeError:
        click.echo("Warning: container DNS not ready yet, continuing anyway...", err=True)

    mount_source = str(ref_path)
    if Path(mount_source).exists():
        runtime.add_disk(
            name, "shared-git", mount_source, f"/shared/git/{mount_name}", readonly=True
        )

    relay_enabled = config.get("relay", {}).get("enabled", False)
    if relay_enabled:
        relay_sock = str(DATA_DIR / "relay.sock")
        if Path(relay_sock).exists():
            runtime.add_device(
                name,
                "bubble-relay",
                "proxy",
                connect=f"unix:{relay_sock}",
                listen="unix:/bubble/relay.sock",
                bind="container",
                uid="1000",
                gid="1000",
            )
            from .relay import generate_relay_token

            token = generate_relay_token(name)
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    f"echo {shlex.quote(token)} > /bubble/relay-token"
                    " && chmod 600 /bubble/relay-token",
                ],
            )


def _clone_and_checkout(runtime, name, t, mount_name, short) -> str:
    """Clone the repo and checkout the appropriate ref. Returns the checkout branch name."""
    url = github_url(t.org_repo)
    click.echo(f"Cloning {t.org_repo} (using shared objects)...")
    runtime.exec(
        name,
        [
            "su",
            "-",
            "user",
            "-c",
            f"git clone --reference /shared/git/{mount_name} {url} /home/user/{short}",
        ],
    )

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
        try:
            runtime.exec(
                name,
                ["su", "-", "user", "-c", f"cd /home/user/{short} && git checkout {q_branch}"],
            )
        except RuntimeError:
            if t.local_path:
                runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"cd /home/user/{short} && git fetch /shared/git/{mount_name}"
                        f" {q_branch}:{q_branch} && git checkout {q_branch}",
                    ],
                )
            else:
                raise
    elif t.kind == "commit":
        click.echo(f"Checking out commit {t.ref[:12]}...")
        q_commit = shlex.quote(t.ref)
        runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{short} && git checkout {q_commit}"],
        )

    return checkout_branch


def _finalize_bubble(
    runtime, name, t, hook, image_name, checkout_branch, short, network, config, ssh, no_interactive
):
    """Post-clone setup: hooks, SSH, network, registration, and attach."""
    project_dir = f"/home/user/{short}"
    if hook:
        hook.post_clone(runtime, name, project_dir)
        ensure_vscode_extensions(hook.vscode_extensions())

    if network:
        extra_domains = hook.network_domains() if hook else None
        _apply_network(runtime, name, config, extra_domains)

    click.echo("Setting up SSH access...")
    _setup_ssh(runtime, name)

    commit = ""
    try:
        commit = runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{short} && git rev-parse HEAD"],
        ).strip()
    except Exception:
        pass
    register_bubble(
        name,
        t.org_repo,
        branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        commit=commit,
        pr=int(t.ref) if t.kind == "pr" else 0,
        base_image=image_name,
    )

    _maybe_install_automation()

    click.echo(f"Bubble '{name}' created successfully.")
    click.echo(f"  SSH: ssh bubble-{name}")

    if not no_interactive:
        if ssh:
            click.echo("Connecting via SSH...")
            subprocess.run(["ssh", f"bubble-{name}"])
        else:
            click.echo("Opening VSCode...")
            open_vscode(name, project_dir)


@main.command("open")
@click.argument("target")
@click.option("--ssh", is_flag=True, help="Drop into SSH session instead of VSCode")
@click.option("--no-interactive", is_flag=True, help="Just create, don't attach")
@click.option("--network/--no-network", default=True, help="Apply network allowlist")
@click.option("--name", "custom_name", type=str, help="Custom container name")
@click.option("--path", "force_path", is_flag=True, help="Interpret target as a local path")
@click.option(
    "--no-clone", is_flag=True, hidden=True, help="Fail if bare repo doesn't exist (used by relay)"
)
def open_cmd(target, ssh, no_interactive, network, custom_name, force_path, no_clone):
    """Open a bubble for a target (GitHub URL, repo, local path, or PR number)."""
    if force_path and not target.startswith(("/", ".", "..")):
        target = "./" + target

    config = load_config()
    runtime = get_runtime(config)

    # Check if target matches an existing container
    existing = _find_existing_container(runtime, target)
    if existing:
        _reattach(runtime, existing, ssh, no_interactive)
        return

    # Parse and register target
    registry = RepoRegistry()
    try:
        t = parse_target(target, registry)
    except TargetParseError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    registry.register(t.owner, t.repo)

    # Generate name and check for existing container with same target
    name = _generate_bubble_name(t, custom_name)
    existing = _find_existing_container(
        runtime,
        target,
        generated_name=name,
        org_repo=t.org_repo,
        kind=t.kind,
        ref=t.ref,
    )
    if existing:
        _reattach(runtime, existing, ssh, no_interactive)
        return

    # Resolve git source, detect language, and build image
    ensure_dirs()
    ref_path, mount_name = _resolve_ref_source(t, no_clone)
    hook, image_name = _detect_and_build_image(runtime, ref_path, t)

    # Deduplicate and create
    existing_names = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing_names)
    click.echo(f"Creating bubble '{name}'...")

    # Provision, clone, and finalize
    short = repo_short_name(t.org_repo)
    _provision_container(runtime, name, image_name, ref_path, mount_name, config)
    checkout_branch = _clone_and_checkout(runtime, name, t, mount_name, short)
    _finalize_bubble(
        runtime,
        name,
        t,
        hook,
        image_name,
        checkout_branch,
        short,
        network,
        config,
        ssh,
        no_interactive,
    )


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
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    try:
        images = runtime.list_images()
        if not images:
            click.echo("No images. Run: bubble images build base")
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
@click.argument("image_name", default="base")
def images_build(image_name):
    """Build an image (base, lean)."""
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


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------


@main.group("relay")
def relay_group():
    """Manage the bubble-in-bubble relay."""


@relay_group.command("enable")
def relay_enable():
    """Enable bubble-in-bubble relay.

    This allows containers to request creation of new bubbles on the host.
    Only repos already cloned in ~/.bubble/git/ can be opened via relay.
    All relay requests are rate-limited and logged.
    """
    click.echo("Enabling bubble-in-bubble relay.")
    click.echo()
    click.echo("This opens a controlled channel from containers to the host.")
    click.echo("Mitigations: known repos only, rate limiting, request logging.")
    click.echo()

    config = load_config()
    config.setdefault("relay", {})["enabled"] = True
    save_config(config)

    # Install and start the relay daemon
    from .automation import install_relay_daemon

    try:
        result = install_relay_daemon()
        if result:
            click.echo(f"  Installed: {result}")
    except Exception as e:
        click.echo(f"  Warning: could not install daemon: {e}")
        click.echo("  You can start it manually with: bubble relay daemon")

    click.echo()
    click.echo("Relay enabled. New bubbles will include the relay socket.")
    click.echo("Existing bubbles need to be recreated to get relay access.")


@relay_group.command("disable")
def relay_disable():
    """Disable bubble-in-bubble relay."""
    config = load_config()
    config.setdefault("relay", {})["enabled"] = False
    save_config(config)

    from .automation import remove_relay_daemon

    try:
        result = remove_relay_daemon()
        if result:
            click.echo(f"  Removed: {result}")
    except Exception:
        pass

    # Remove socket
    from .relay import RELAY_SOCK

    RELAY_SOCK.unlink(missing_ok=True)

    click.echo("Relay disabled.")


@relay_group.command("status")
def relay_status():
    """Show relay status."""
    config = load_config()
    enabled = config.get("relay", {}).get("enabled", False)
    click.echo(f"  Relay: {'enabled' if enabled else 'disabled'}")

    from .relay import RELAY_SOCK

    click.echo(f"  Socket: {'exists' if RELAY_SOCK.exists() else 'not found'}")

    from .relay import RELAY_LOG

    if RELAY_LOG.exists():
        # Show last 5 log entries
        lines = RELAY_LOG.read_text().strip().splitlines()
        if lines:
            click.echo(f"  Log ({len(lines)} entries, last 5):")
            for line in lines[-5:]:
                click.echo(f"    {line}")
        else:
            click.echo("  Log: empty")
    else:
        click.echo("  Log: no requests yet")


@relay_group.command("daemon")
def relay_daemon_cmd():
    """Run the relay daemon (used by launchd/systemd)."""
    from .relay import run_daemon

    run_daemon()


if __name__ == "__main__":
    main()
