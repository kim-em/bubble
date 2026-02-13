"""CLI entry point for lean-bubbles."""

import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click

from . import __version__
from .config import (
    GIT_DIR,
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
        keys_str = "\\n".join(pub_keys)
        runtime.exec(name, [
            "su", "-", "lean", "-c",
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '{keys_str}' > ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys",
        ])

    add_ssh_config(name)


def _detect_project_dir(runtime: ContainerRuntime, name: str) -> str:
    """Detect the project directory inside a container."""
    try:
        return runtime.exec(name, [
            "bash", "-c", "ls -d /home/lean/*/ 2>/dev/null | head -1"
        ]).strip().rstrip("/")
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

    # Install launchd jobs on macOS
    if platform.system() == "Darwin":
        _install_launchd_jobs()

    click.echo()
    click.echo("Setup complete! Try: bubble new batteries --pr 1234")


def _install_launchd_jobs():
    """Install launchd plists for automated git update and image refresh."""
    plist_dir = Path(__file__).parent.parent / "config"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)

    for plist_name in ["com.lean-bubbles.git-update.plist",
                       "com.lean-bubbles.image-refresh.plist"]:
        src = plist_dir / plist_name
        dst = launch_agents / plist_name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            subprocess.run(["launchctl", "load", str(dst)],
                           capture_output=True)
            click.echo(f"  Installed launchd job: {plist_name}")


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

    # Mount shared git store
    if GIT_DIR.exists():
        runtime.add_disk(name, "shared-git", str(GIT_DIR), "/shared/git", readonly=True)

    # Ensure bare repo exists for this project
    bare_path = ensure_repo(org_repo)

    # Clone the repo inside the container using --reference
    url = github_url(org_repo)
    click.echo(f"Cloning {org_repo} (using shared objects)...")
    runtime.exec(name, [
        "su", "-", "lean", "-c",
        f"git clone --reference /shared/git/{bare_path.name} {url} /home/lean/{short}",
    ])

    # Checkout PR or branch
    checkout_branch = ""
    if pr:
        click.echo(f"Checking out PR #{pr}...")
        checkout_branch = f"pr-{pr}"
        runtime.exec(name, [
            "su", "-", "lean", "-c",
            f"cd /home/lean/{short} && git fetch origin pull/{pr}/head:pr-{pr} && git checkout pr-{pr}",
        ])
    elif branch:
        click.echo(f"Checking out branch '{branch}'...")
        checkout_branch = branch
        runtime.exec(name, [
            "su", "-", "lean", "-c",
            f"cd /home/lean/{short} && git checkout {branch}",
        ])

    # Inject .lake cache if available
    from .lake_cache import inject_cache_into_container
    project_dir = f"/home/lean/{short}"
    if inject_cache_into_container(runtime, name, project_dir, short):
        click.echo("  Injected cached .lake artifacts.")

    # Apply network allowlist
    if network and config.get("network", {}).get("allowlist"):
        try:
            from .network import apply_allowlist
            apply_allowlist(runtime, name, config["network"]["allowlist"])
            click.echo("  Network allowlist applied.")
        except Exception as e:
            click.echo(f"  Warning: could not apply network allowlist: {e}")

    # Set up SSH access
    click.echo("Setting up SSH access...")
    _setup_ssh(runtime, name)

    # Register in lifecycle
    from .lifecycle import register_bubble
    commit = ""
    try:
        commit = runtime.exec(name, [
            "su", "-", "lean", "-c",
            f"cd /home/lean/{short} && git rev-parse HEAD",
        ]).strip()
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
@click.option("--copy", "copy_mode", is_flag=True, help="Copy instead of move (leave local unchanged)")
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
        runtime, directory, config,
        pr=pr or 0, copy_mode=copy_mode, custom_name=name or "",
    )

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
    from .lifecycle import check_git_synced, archive_bubble
    synced, reason = check_git_synced(runtime, name, project_dir)
    if not synced and not force:
        click.echo(f"Cannot archive: {reason}", err=True)
        click.echo("Use --force to archive anyway, or push/commit first.", err=True)
        sys.exit(1)
    elif not synced:
        click.echo(f"Warning: {reason}")

    # Extract Claude sessions before archiving
    ext_config = config.get("extensions", {}).get("claude", {})
    if ext_config.get("enabled"):
        from .extensions.claude import extract_sessions
        session_dir = extract_sessions(runtime, name, name)
        if session_dir:
            click.echo(f"  Claude sessions saved to {session_dir}")

    state = archive_bubble(runtime, name, project_dir)
    remove_ssh_config(name)

    click.echo(f"Bubble '{name}' archived.")
    click.echo(f"  Repo: {state.get('org_repo', '')}")
    click.echo(f"  Branch: {state.get('branch', '')}")
    click.echo(f"  Commit: {state.get('commit', '')[:12]}")
    click.echo(f"Resume with: bubble resume {name}")


@main.command()
@click.argument("name_or_url")
@click.option("--no-attach", is_flag=True, help="Don't open VSCode after resuming")
def resume(name_or_url, no_attach):
    """Resume an archived bubble or reconstitute from a PR URL."""
    config = load_config()
    ensure_platform(config)
    runtime = get_runtime(config)

    state = None
    name = name_or_url

    # Check if it's a PR URL
    if name_or_url.startswith("http") or "#" in name_or_url:
        from .pr_metadata import extract_metadata, parse_pr_url
        try:
            org_repo, pr_num = parse_pr_url(name_or_url)
            click.echo(f"Looking up PR metadata for {org_repo}#{pr_num}...")
            state = extract_metadata(name_or_url)
            if not state:
                click.echo("No lean-bubbles metadata found in PR description.", err=True)
                click.echo("Creating fresh bubble from PR instead...")
                # Fall back to creating a new bubble
                short = repo_short_name(org_repo)
                name = generate_name(short, "pr", str(pr_num))
                state = {
                    "org_repo": org_repo,
                    "pr": pr_num,
                    "branch": f"pr-{pr_num}",
                    "base_image": "lean-base",
                }
            else:
                name = generate_name(
                    repo_short_name(state.get("org_repo", "")),
                    "pr", str(state.get("pr", pr_num)),
                )
        except ValueError as e:
            click.echo(f"Error parsing URL: {e}", err=True)
            sys.exit(1)
    else:
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

    # Inject Claude sessions
    ext_config = config.get("extensions", {}).get("claude", {})
    if ext_config.get("enabled"):
        from .extensions.claude import inject_sessions
        if inject_sessions(runtime, name, name):
            click.echo("  Claude sessions restored.")

    _setup_ssh(runtime, name)

    short = repo_short_name(state.get("org_repo", ""))
    project_dir = f"/home/lean/{short}"
    click.echo(f"Bubble '{name}' resumed.")

    if not no_attach:
        click.echo("Opening VSCode...")
        open_vscode(name, project_dir)


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------

@main.command()
@click.argument("name")
def claude(name):
    """Start or resume Claude Code inside a bubble."""
    config = load_config()
    runtime = get_runtime(config)
    _ensure_running(runtime, name)

    project_dir = _detect_project_dir(runtime, name)
    ext_config = config.get("extensions", {}).get("claude", {})

    # Check if there's a saved session to resume
    from .extensions.claude import find_session_id, start_claude_in_container
    session_id = find_session_id(name)
    if session_id:
        click.echo(f"Resuming Claude session {session_id[:12]}...")
    else:
        click.echo("Starting new Claude session...")

    start_claude_in_container(
        runtime, name, project_dir,
        session_id=session_id,
        unset_api_key=ext_config.get("unset_api_key", True),
    )


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
            capture_output=True, text=True, check=True,
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
# claude-skill
# ---------------------------------------------------------------------------

@main.command("claude-skill")
def install_claude_skill():
    """Install the lean-bubbles Claude Code skill."""
    skill_src = Path(__file__).parent.parent / "claude-skill" / "SKILL.md"
    skill_dst = Path.home() / ".claude" / "skills" / "lean-bubbles"

    if not skill_src.exists():
        click.echo("Error: skill file not found in package.", err=True)
        sys.exit(1)

    skill_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_src, skill_dst / "SKILL.md")
    click.echo(f"Claude Code skill installed to {skill_dst}/SKILL.md")
    click.echo("Claude will now know how to use bubble commands in your sessions.")


if __name__ == "__main__":
    main()
