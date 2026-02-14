"""CLI entry point for lean-bubbles."""

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
from .config import (
    ensure_dirs,
    load_config,
    repo_short_name,
    resolve_repo,
)
from .git_store import bare_repo_path, ensure_repo, github_url, update_all_repos
from .naming import deduplicate_name, generate_name
from .runtime.base import ContainerRuntime
from .runtime.incus import IncusRuntime
from .vscode import (
    add_ssh_config,
    ensure_vscode_extensions,
    open_vscode,
    remove_ssh_config,
)


def get_runtime(config: dict) -> ContainerRuntime:
    """Get the configured container runtime."""
    backend = config["runtime"]["backend"]
    if backend == "incus":
        return IncusRuntime()
    raise ValueError(f"Unknown runtime backend: {backend}")


def ensure_platform(config: dict):
    """Ensure the platform is ready (Colima on macOS, native on Linux)."""
    if platform.system() == "Darwin":
        from .runtime.colima import ensure_colima

        rt = config["runtime"]
        ensure_colima(
            cpu=rt["colima_cpu"],
            memory=rt["colima_memory"],
            disk=rt.get("colima_disk", 60),
            vm_type=rt.get("colima_vm_type", "vz"),
        )


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
        # Write keys to temp file and push via incus file push (avoids shell injection)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".keys", delete=False) as f:
            f.write("\n".join(pub_keys) + "\n")
            tmp_keys = f.name
        try:
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "lean",
                    "-c",
                    "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                ],
            )
            subprocess.run(
                ["incus", "file", "push", tmp_keys, f"{name}/home/lean/.ssh/authorized_keys"],
                check=True,
                capture_output=True,
            )
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    "chown lean:lean /home/lean/.ssh/authorized_keys"
                    " && chmod 600 /home/lean/.ssh/authorized_keys",
                ],
            )
        finally:
            Path(tmp_keys).unlink(missing_ok=True)

    add_ssh_config(name)


def _apply_network(runtime: ContainerRuntime, name: str, config: dict):
    """Apply network allowlist to a container if configured."""
    domains = config.get("network", {}).get("allowlist", [])
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
            runtime.exec(name, ["bash", "-c", "ls -d /home/lean/*/ 2>/dev/null | head -1"])
            .strip()
            .rstrip("/")
        )
    except Exception:
        return "/home/lean"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(__version__)
def main():
    """lean-bubbles: Containerized Lean development environments."""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command()
def init():
    """First-time setup: configure runtime, build base image, init git store."""
    config = load_config()
    ensure_dirs()

    # Platform setup
    click.echo("Setting up container runtime...")
    ensure_platform(config)

    runtime = get_runtime(config)
    if not runtime.is_available():
        click.echo("Error: Incus is not available. Ensure Colima is running.", err=True)
        sys.exit(1)
    click.echo("  Runtime ready.")

    # VSCode extensions
    ensure_vscode_extensions()

    # Build lean-base image if needed
    if not runtime.image_exists("lean-base"):
        click.echo("Building lean-base image...")
        from .images.builder import build_lean_base

        build_lean_base(runtime)
    else:
        click.echo("  lean-base image already exists.")

    # Init shared git store
    click.echo("Initializing shared git store...")
    for repo in config["git"]["shared_repos"]:
        path = bare_repo_path(repo)
        if path.exists():
            click.echo(f"  {repo}: already exists")
        else:
            click.echo(f"  {repo}: cloning bare mirror...")
            ensure_repo(repo)

    # Offer to install automation
    from .automation import install_automation, is_automation_installed

    status = is_automation_installed()
    if not any(status.values()):
        if click.confirm(
            "Install automation (hourly git update, weekly image refresh)?", default=True
        ):
            installed = install_automation()
            for item in installed:
                click.echo(f"  Installed: {item}")
    else:
        click.echo("  Automation already installed.")

    click.echo()
    click.echo("Setup complete! Try: bubble new batteries --pr 1234")


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


@main.command("new")
@click.argument("repo")
@click.option("--pr", type=int, help="PR number to checkout")
@click.option("--branch", type=str, help="Branch to checkout")
@click.option("--name", type=str, help="Custom container name")
@click.option("--no-attach", is_flag=True, help="Don't open VSCode after creation")
@click.option("--network/--no-network", default=True, help="Apply network allowlist")
def new_bubble(repo, pr, branch, name, no_attach, network):
    """Create a new bubble for a Lean project."""
    config = load_config()
    ensure_platform(config)
    runtime = get_runtime(config)

    # Resolve repo name
    org_repo = resolve_repo(repo)
    short = repo_short_name(org_repo)

    # Generate name
    if not name:
        if pr:
            name = generate_name(short, "pr", str(pr))
        elif branch:
            name = generate_name(short, "branch", branch)
        else:
            name = generate_name(short, "main", "")

    # Deduplicate
    existing = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing)

    click.echo(f"Creating bubble '{name}'...")

    # Ensure lean-base image exists
    if not runtime.image_exists("lean-base"):
        click.echo("Building lean-base image first...")
        from .images.builder import build_lean_base

        build_lean_base(runtime)

    # Launch container
    runtime.launch(name, "lean-base")

    # Wait for container to be ready (including DNS)
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

    # Mount only the needed bare repo (not the entire git store)
    bare_path = ensure_repo(org_repo)
    if bare_path.exists():
        runtime.add_disk(
            name, "shared-git", str(bare_path), f"/shared/git/{bare_path.name}", readonly=True
        )

    # Clone the repo inside the container using --reference
    url = github_url(org_repo)
    click.echo(f"Cloning {org_repo} (using shared objects)...")
    runtime.exec(
        name,
        [
            "su",
            "-",
            "lean",
            "-c",
            f"git clone --reference /shared/git/{bare_path.name} {url} /home/lean/{short}",
        ],
    )

    # Checkout PR or branch
    checkout_branch = ""
    if pr:
        click.echo(f"Checking out PR #{pr}...")
        checkout_branch = f"pr-{pr}"
        q_branch = shlex.quote(checkout_branch)
        runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd /home/lean/{short} && git fetch origin pull/{pr}/head:{q_branch}"
                f" && git checkout {q_branch}",
            ],
        )
    elif branch:
        click.echo(f"Checking out branch '{branch}'...")
        checkout_branch = branch
        q_branch = shlex.quote(branch)
        runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd /home/lean/{short} && git checkout {q_branch}",
            ],
        )

    # Inject .lake cache if available
    from .lake_cache import inject_cache_into_container

    project_dir = f"/home/lean/{short}"
    if inject_cache_into_container(runtime, name, project_dir, short):
        click.echo("  Injected cached .lake artifacts.")

    # Apply network allowlist
    if network:
        _apply_network(runtime, name, config)

    # Set up SSH access
    click.echo("Setting up SSH access...")
    _setup_ssh(runtime, name)

    # Register in lifecycle
    from .lifecycle import register_bubble

    commit = ""
    try:
        commit = runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd /home/lean/{short} && git rev-parse HEAD",
            ],
        ).strip()
    except Exception:
        pass
    register_bubble(name, org_repo, branch=checkout_branch, commit=commit, pr=pr or 0)

    click.echo(f"Bubble '{name}' created successfully.")
    click.echo(f"  SSH: ssh bubble-{name}")
    click.echo(f"  Shell: bubble shell {name}")

    if not no_attach:
        click.echo("Opening VSCode...")
        open_vscode(name, project_dir)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--archived", is_flag=True, help="Include archived bubbles")
def list_bubbles(as_json, archived):
    """List all bubbles."""
    config = load_config()
    runtime = get_runtime(config)

    containers = runtime.list_containers()

    # Merge with archived info from registry
    from .lifecycle import _load_registry

    registry = _load_registry()
    archived_bubbles = []
    if archived:
        for bname, binfo in registry.get("bubbles", {}).items():
            if binfo.get("state") == "archived":
                archived_bubbles.append(binfo | {"name": bname})

    if as_json:
        data = [{"name": c.name, "state": c.state, "ipv4": c.ipv4} for c in containers]
        if archived:
            data.extend(archived_bubbles)
        click.echo(json.dumps(data, indent=2))
        return

    if not containers and not archived_bubbles:
        click.echo("No bubbles. Create one with: bubble new mathlib4")
        return

    if containers:
        click.echo(f"{'NAME':<30} {'STATE':<10} {'IPv4':<16}")
        click.echo("-" * 56)
        for c in containers:
            click.echo(f"{c.name:<30} {c.state:<10} {c.ipv4 or '-':<16}")

    if archived_bubbles:
        click.echo()
        click.echo(f"{'ARCHIVED':<30} {'REPO':<25} {'BRANCH':<20}")
        click.echo("-" * 75)
        for b in archived_bubbles:
            click.echo(f"{b['name']:<30} {b.get('org_repo', ''):<25} {b.get('branch', ''):<20}")


# ---------------------------------------------------------------------------
# attach / shell
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def attach(name):
    """Open VSCode connected to a bubble."""
    config = load_config()
    runtime = get_runtime(config)
    _ensure_running(runtime, name)

    project_dir = _detect_project_dir(runtime, name)
    click.echo(f"Opening VSCode for '{name}'...")
    open_vscode(name, project_dir)


@main.command()
@click.argument("name")
def shell(name):
    """Open a shell in a bubble."""
    config = load_config()
    runtime = get_runtime(config)
    _ensure_running(runtime, name)
    subprocess.run(["incus", "exec", name, "--", "su", "-", "lean"])


# ---------------------------------------------------------------------------
# pause / destroy
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def pause(name):
    """Pause (freeze) a bubble."""
    config = load_config()
    runtime = get_runtime(config)
    runtime.freeze(name)
    click.echo(f"Bubble '{name}' paused.")


@main.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Force destroy even if running")
def destroy(name, force):
    """Destroy a bubble permanently."""
    config = load_config()
    runtime = get_runtime(config)

    if not force:
        click.confirm(f"Permanently destroy bubble '{name}'?", abort=True)

    runtime.delete(name, force=True)
    remove_ssh_config(name)
    click.echo(f"Bubble '{name}' destroyed.")


# ---------------------------------------------------------------------------
# wrap
# ---------------------------------------------------------------------------


@main.command("wrap")
@click.argument("directory", default=".", type=click.Path(exists=True))
@click.option("--pr", type=int, help="Associate with a PR number")
@click.option(
    "--copy", "copy_mode", is_flag=True, help="Copy instead of move (leave local unchanged)"
)
@click.option("--name", type=str, help="Custom container name")
@click.option("--no-attach", is_flag=True, help="Don't open VSCode after wrapping")
def wrap_cmd(directory, pr, copy_mode, name, no_attach):
    """Move (or copy) a local working directory into a bubble."""
    config = load_config()
    ensure_platform(config)
    runtime = get_runtime(config)

    from .wrap import detect_repo_info, wrap_directory

    directory = Path(directory).resolve()
    info = detect_repo_info(directory)

    click.echo(f"Detected: {info['org_repo']} on branch '{info['branch']}'")
    if info["has_changes"]:
        if copy_mode:
            click.echo("  (copying with uncommitted changes)")
        else:
            click.echo("  (will stash uncommitted changes locally)")

    bubble_name = wrap_directory(
        runtime,
        directory,
        config,
        pr=pr or 0,
        copy_mode=copy_mode,
        custom_name=name or "",
    )

    _apply_network(runtime, bubble_name, config)
    add_ssh_config(bubble_name)
    click.echo(f"Bubble '{bubble_name}' created from local directory.")
    click.echo(f"  SSH: ssh bubble-{bubble_name}")

    if not no_attach:
        short = info["short"]
        click.echo("Opening VSCode...")
        open_vscode(bubble_name, f"/home/lean/{short}")


# ---------------------------------------------------------------------------
# archive / resume
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Archive even if git is not fully synced")
def archive(name, force):
    """Archive a bubble (save state, destroy container)."""
    config = load_config()
    runtime = get_runtime(config)

    _ensure_running(runtime, name)
    project_dir = _detect_project_dir(runtime, name)

    # Check git sync state
    from .lifecycle import archive_bubble, check_git_synced

    synced, reason = check_git_synced(runtime, name, project_dir)
    if not synced and not force:
        click.echo(f"Cannot archive: {reason}", err=True)
        click.echo("Use --force to archive anyway, or push/commit first.", err=True)
        sys.exit(1)
    elif not synced:
        click.echo(f"Warning: {reason}")

    state = archive_bubble(runtime, name, project_dir)
    remove_ssh_config(name)

    click.echo(f"Bubble '{name}' archived.")
    click.echo(f"  Repo: {state.get('org_repo', '')}")
    click.echo(f"  Branch: {state.get('branch', '')}")
    click.echo(f"  Commit: {state.get('commit', '')[:12]}")
    click.echo(f"Resume with: bubble resume {name}")


@main.command()
@click.argument("name")
@click.option("--no-attach", is_flag=True, help="Don't open VSCode after resuming")
def resume(name, no_attach):
    """Resume an archived bubble from the local registry."""
    config = load_config()
    ensure_platform(config)
    runtime = get_runtime(config)

    # Look up from local registry
    from .lifecycle import get_bubble_info

    state = get_bubble_info(name)
    if not state:
        click.echo(f"No archived bubble '{name}' found.", err=True)
        sys.exit(1)
    if state.get("state") != "archived":
        click.echo(f"Bubble '{name}' is not archived (state: {state.get('state')}).", err=True)
        sys.exit(1)

    # Deduplicate name
    existing = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing)

    click.echo(f"Reconstituting bubble '{name}'...")
    from .lifecycle import reconstitute_bubble

    reconstitute_bubble(runtime, name, state)

    _apply_network(runtime, name, config)
    _setup_ssh(runtime, name)

    short = repo_short_name(state.get("org_repo", ""))
    project_dir = f"/home/lean/{short}"
    click.echo(f"Bubble '{name}' resumed.")

    if not no_attach:
        click.echo("Opening VSCode...")
        open_vscode(name, project_dir)


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
            click.echo("No images. Run: bubble init")
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
@click.argument("image_name", default="lean-base")
def images_build(image_name):
    """Build a base image (lean-base, lean-mathlib, lean-batteries, lean-lean4)."""
    config = load_config()
    ensure_platform(config)
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
    config = load_config()
    update_all_repos(config["git"]["shared_repos"])
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
    runtime = get_runtime(config)
    _ensure_running(runtime, name)

    domains = config.get("network", {}).get("allowlist", [])
    if not domains:
        click.echo("No domains in allowlist. Edit ~/.lean-bubbles/config.toml", err=True)
        sys.exit(1)

    from .network import apply_allowlist

    apply_allowlist(runtime, name, domains)
    click.echo(f"Network allowlist applied to '{name}' ({len(domains)} domains).")


@network_group.command("remove")
@click.argument("name")
def network_remove(name):
    """Remove network restrictions from a bubble."""
    config = load_config()
    runtime = get_runtime(config)
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
