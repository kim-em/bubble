"""CLI entry point for bubble."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import click

from . import __version__
from .clone import clone_and_checkout
from .config import (
    claude_config_mounts,
    editor_config_mounts,
    ensure_dirs,
    load_config,
    maybe_symlink_claude_projects,
    parse_mounts,
    repo_short_name,
    save_config,
)
from .container_helpers import (
    detect_project_dir,
    ensure_running,
    find_existing_container,
    get_host_git_identity,
)
from .finalization import (
    echo_editor_opening,
    finalize_bubble,
    inject_local_ssh_keys,
    machine_readable_output,
)
from .git_store import (
    bare_repo_path,
    ensure_rev_available,
    fetch_ref,
    init_bare_repo,
    update_all_repos,
)
from .image_management import (
    detect_and_build_image,
    maybe_rebuild_base_image,
    maybe_rebuild_customize,
    maybe_rebuild_tools,
)
from .lifecycle import load_registry, register_bubble, unregister_bubble
from .naming import deduplicate_name, generate_name
from .native import open_native
from .provisioning import mount_overlaps, provision_container
from .repo_registry import RepoRegistry
from .security import SETTINGS as SECURITY_SETTINGS
from .security import (
    VALID_VALUES as SECURITY_VALID_VALUES,
)
from .security import (
    get_setting,
    is_enabled,
    is_locked_off,
    print_warnings,
)
from .setup import get_runtime
from .target import Target, TargetParseError, parse_target
from .vscode import (
    SSH_CONFIG_FILE,
    add_ssh_config,
    open_editor,
    remove_ssh_config,
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


BASIC_COMMANDS = {"list", "pause", "pop"}


class BubbleGroup(click.Group):
    """Custom group that routes unknown first args to the implicit 'open' command."""

    def format_usage(self, ctx, formatter):
        formatter.write("Usage: bubble TARGET [OPTIONS]\n")
        formatter.write("       bubble COMMAND [ARGS]...\n")

    def format_commands(self, ctx, formatter):
        """Split commands into Basic and Advanced sections."""
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.commands.get(subcommand)
            if cmd is None or cmd.hidden:
                continue
            help_text = cmd.get_short_help_str(limit=formatter.width)
            commands.append((subcommand, help_text))

        basic = [(n, h) for n, h in commands if n in BASIC_COMMANDS]
        advanced = [(n, h) for n, h in commands if n not in BASIC_COMMANDS]

        if basic:
            with formatter.section("Commands"):
                formatter.write_dl(basic)
        if advanced:
            with formatter.section("Advanced"):
                formatter.write_dl(advanced)

    def parse_args(self, ctx, args):
        """If no known command is found among args, prepend 'open'.

        This supports both `bubble TARGET` and `bubble --ssh HOST TARGET`.
        """
        has_command = any(not a.startswith("-") and a in self.commands for a in args)
        has_non_option = any(not a.startswith("-") for a in args)
        if args and has_non_option and not has_command:
            args = ["open"] + args
        return super().parse_args(ctx, args)


@click.group(cls=BubbleGroup, context_settings=dict(help_option_names=["-h", "--help"]))
@click.version_option(__version__)
def main():
    """bubble: Open a containerized dev environment.

    Run bubble TARGET to create (or reattach to) an isolated container and
    open it in VSCode via Remote SSH. Use --shell for a plain SSH session.

    \b
    Examples:
      bubble .                                      Current directory
      bubble leanprover-community/mathlib4          GitHub repo
      bubble https://github.com/owner/repo/pull/42  Pull request
      bubble mathlib4/pull/123                      PR shorthand
      bubble 456                                    PR in current repo
    """


@main.command("help", hidden=True)
@click.argument("command", nargs=-1)
@click.pass_context
def help_cmd(ctx, command):
    """Show help for a command."""
    if not command:
        click.echo(main.get_help(ctx))
        return
    cmd = main
    for name in command:
        if isinstance(cmd, click.Group):
            cmd = cmd.get_command(ctx, name)
            if cmd is None:
                click.echo(f"Unknown command: {' '.join(command)}")
                raise SystemExit(1)
        else:
            click.echo(f"'{name}' is not a subcommand of '{command[command.index(name) - 1]}'")
            raise SystemExit(1)
    # Build a proper context so the Usage line shows the right command name
    sub_ctx = click.Context(cmd, info_name=command[-1], parent=ctx.parent)
    click.echo(cmd.get_help(sub_ctx))


# ---------------------------------------------------------------------------
# open (the primary command, invoked implicitly)
# ---------------------------------------------------------------------------


def _generate_bubble_name(t, custom_name: str | None) -> str:
    """Generate a container name from a parsed target."""
    if custom_name:
        return custom_name
    if t.kind == "pr":
        return generate_name(t.short_name, "pr", t.ref)
    if t.kind == "issue":
        return generate_name(t.short_name, "issue", t.ref)
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
            bare_path = init_bare_repo(t.org_repo)
        ref_path = bare_path
        mount_name = ref_path.name

        if t.kind == "pr":
            click.echo(f"Fetching PR #{t.ref}...")
            try:
                fetch_ref(t.org_repo, f"refs/pull/{t.ref}/head:refs/pull/{t.ref}/head")
            except Exception:
                pass  # May already be available from a full fetch

    return ref_path, mount_name


def _open_remote(
    remote_host,
    target,
    editor,
    no_interactive,
    network,
    custom_name,
    config,
    git_name="",
    git_email="",
    command=None,
    claude_config=True,
    new_branch=None,
    base_ref=None,
    gh_token=False,
):
    """Open a bubble on a remote host, then connect locally."""
    from .remote import remote_open

    try:
        result = remote_open(
            remote_host,
            target,
            network=network,
            custom_name=custom_name,
            git_name=git_name,
            git_email=git_email,
            claude_config=claude_config,
            new_branch=new_branch,
            base_ref=base_ref,
        )
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    name = result["name"]
    project_dir = result.get("project_dir", "/home/user")
    workspace_file = result.get("workspace_file")
    org_repo = result.get("org_repo", "")

    # Inject local SSH keys into the container so the chained ProxyCommand works
    inject_local_ssh_keys(remote_host, name)

    # Inject GitHub token from local host into the remote container
    if gh_token:
        from .github_token import setup_gh_token

        setup_gh_token(None, name, remote_host=remote_host)

    # Write local SSH config with chained ProxyCommand through the remote host
    host_key = is_enabled(config, "host_key_trust")
    add_ssh_config(name, remote_host=remote_host, host_key_trust=host_key)

    # Register in local lifecycle registry with remote_host info
    register_bubble(
        name,
        org_repo,
        branch=result.get("branch", ""),
        base_image=result.get("image", ""),
        remote_host=remote_host.spec_string(),
    )

    click.echo(f"Bubble '{name}' ready on {remote_host.ssh_destination}.")
    click.echo(f"  SSH: ssh bubble-{name}")

    if not no_interactive:
        echo_editor_opening(editor)
        open_editor(editor, name, project_dir, workspace_file=workspace_file, command=command)


def _reattach(runtime, name, editor, no_interactive, command=None):
    """Re-attach to an existing container."""
    ensure_running(runtime, name)

    if no_interactive:
        click.echo(f"Bubble '{name}' is running.")
        return

    project_dir = detect_project_dir(runtime, name)

    # Pull latest if the working tree is clean
    if project_dir:
        try:
            q_dir = shlex.quote(project_dir)
            status = runtime.exec(
                name,
                ["su", "-", "user", "-c", f"cd {q_dir} && git status --porcelain -uno"],
            ).strip()
            if not status:
                # Only pull if there's an upstream tracking branch
                has_upstream = runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"cd {q_dir} && git rev-parse --abbrev-ref"
                        " @{upstream} 2>/dev/null || true",
                    ],
                ).strip()
                if has_upstream:
                    click.echo("Working tree is clean, pulling latest...")
                    try:
                        runtime.exec(
                            name,
                            ["su", "-", "user", "-c", f"cd {q_dir} && git pull --ff-only"],
                        )
                    except RuntimeError:
                        pass  # Silently continue if pull fails
        except RuntimeError:
            pass  # Can't check status, skip pull

    echo_editor_opening(editor)
    open_editor(editor, name, project_dir, command=command)


# The "open" command is hidden from help because users invoke it implicitly via
# `bubble TARGET`. It exists as an explicit subcommand because remote.py calls
# `bubble open --no-interactive --machine-readable` on remote hosts.
@main.command("open", hidden=True)
@click.argument("target")
@click.option(
    "--editor",
    "editor_choice",
    type=click.Choice(["vscode", "emacs", "neovim", "shell"]),
    default=None,
    help="Editor to use (default: from config or vscode)",
)
@click.option("--shell", is_flag=True, help="Drop into SSH session (shortcut for --editor shell)")
@click.option("--emacs", is_flag=True, help="Use Emacs (shortcut for --editor emacs)")
@click.option("--neovim", is_flag=True, help="Use Neovim (shortcut for --editor neovim)")
@click.option(
    "--ssh",
    "ssh_host",
    type=str,
    default=None,
    metavar="HOST",
    help="Run on remote host (host, user@host, or user@host:port)",
)
@click.option("--cloud", "cloud", is_flag=True, help="Run on auto-provisioned Hetzner Cloud server")
@click.option(
    "--local",
    "force_local",
    is_flag=True,
    help="Force local execution (override default remote/cloud)",
)
@click.option("--no-interactive", is_flag=True, help="Just create, don't attach")
@click.option(
    "--machine-readable", is_flag=True, hidden=True, help="Output JSON (for remote orchestration)"
)
@click.option("--network/--no-network", default=True, help="Apply network allowlist")
@click.option("--name", "custom_name", type=str, help="Custom container name")
@click.option(
    "--command",
    "command",
    type=str,
    default=None,
    help="Run a command via SSH instead of interactive shell",
)
@click.option(
    "--native",
    is_flag=True,
    help="Non-containerized workspace (local clone, no isolation)",
)
@click.option("--path", "force_path", is_flag=True, help="Interpret target as a local path")
@click.option(
    "-b",
    "--new-branch",
    "new_branch",
    type=str,
    default=None,
    help="Create a new branch with this name",
)
@click.option(
    "--base",
    "base_ref",
    type=str,
    default=None,
    help="Base branch for --new-branch (default: repo default branch)",
)
@click.option(
    "--no-clone", is_flag=True, hidden=True, help="Fail if bare repo doesn't exist (used by relay)"
)
@click.option("--git-name", type=str, default=None, hidden=True, help="Git user.name for container")
@click.option(
    "--git-email",
    type=str,
    default=None,
    hidden=True,
    help="Git user.email for container",
)
@click.option(
    "--mount",
    "mounts",
    type=str,
    multiple=True,
    help="Mount host dir: /host/path:/container/path[:ro|rw] (repeatable)",
)
@click.option(
    "--claude-config/--no-claude-config",
    default=True,
    help="Mount ~/.claude config read-only into container (default: enabled)",
)
@click.option(
    "--claude-credentials/--no-claude-credentials",
    default=None,
    help="Mount ~/.claude credentials into container (default: from config or disabled)",
)
@click.option(
    "--gh-token/--no-gh-token",
    default=None,
    help="Inject GitHub auth token into container (default: from config or disabled)",
)
def open_cmd(
    target,
    editor_choice,
    shell,
    emacs,
    neovim,
    ssh_host,
    cloud,
    force_local,
    no_interactive,
    machine_readable,
    network,
    custom_name,
    command,
    native,
    force_path,
    new_branch,
    base_ref,
    no_clone,
    git_name,
    git_email,
    mounts,
    claude_config,
    claude_credentials,
    gh_token,
):
    """Open a bubble for a target (GitHub URL, repo, local path, or PR number)."""
    if force_path and not target.startswith(("/", ".", "..")):
        target = "./" + target

    config = load_config()

    # Enforce security: reject all user mounts when user_mounts is locked off
    # This covers both --mount CLI flags and [[mounts]] from config
    user_mounts_locked = is_locked_off(config, "user_mounts")
    if user_mounts_locked and (mounts or config.get("mounts")):
        click.echo(
            "Error: user mounts rejected because security.user_mounts=off. "
            "Re-enable: bubble config set security.user_mounts on",
            err=True,
        )
        sys.exit(1)

    # Enforce security: reject --claude-credentials when locked off
    if claude_credentials and is_locked_off(config, "claude_credentials"):
        click.echo(
            "Error: --claude-credentials rejected because security.claude_credentials=off. "
            "Re-enable: bubble config set security.claude_credentials on",
            err=True,
        )
        sys.exit(1)

    # Parse user mounts from config + CLI flags
    try:
        mount_specs = parse_mounts(config, mounts)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    # Validate mount sources exist
    for m in mount_specs:
        if not Path(m.source).exists():
            click.echo(f"Mount source does not exist: {m.source}", err=True)
            sys.exit(1)

    # Auto-detect git identity from host if not explicitly provided
    if git_name is None or git_email is None:
        auto_name, auto_email = get_host_git_identity()
        if git_name is None:
            git_name = auto_name
        if git_email is None:
            git_email = auto_email

    # Parse --command into a list
    command_args = shlex.split(command) if command else None

    # Resolve editor: shortcut flags > --editor > config > vscode
    valid_editors = ("vscode", "emacs", "neovim", "shell")
    if command_args:
        editor = "shell"
    elif shell:
        editor = "shell"
    elif emacs:
        editor = "emacs"
    elif neovim:
        editor = "neovim"
    elif editor_choice is not None:
        editor = editor_choice
    else:
        editor = config.get("editor", "vscode")
        if editor not in valid_editors:
            click.echo(f"Warning: unknown editor '{editor}' in config, using vscode.", err=True)
            editor = "vscode"

    # Print security posture warnings (for auto settings)
    if not machine_readable:
        print_warnings(config)

    # Native mode: skip all container/remote logic
    if native:
        incompatible = []
        if ssh_host:
            incompatible.append("--ssh")
        if cloud:
            incompatible.append("--cloud")
        if not network:
            incompatible.append("--no-network")
        if machine_readable:
            incompatible.append("--machine-readable")
        if incompatible:
            click.echo(
                f"--native cannot be combined with {', '.join(incompatible)}",
                err=True,
            )
            sys.exit(1)
        open_native(target, editor, no_interactive, custom_name, command=command_args)
        return

    # Priority: --local > --ssh > --cloud > [cloud] default > [remote] default_host
    remote_host = None
    if not force_local and not machine_readable:
        if ssh_host:
            from .remote import RemoteHost

            remote_host = RemoteHost.parse(ssh_host)
        elif cloud or config.get("cloud", {}).get("default", False):
            if is_locked_off(config, "cloud_root"):
                click.echo(
                    "Error: cloud access rejected because security.cloud_root=off. "
                    "Re-enable: bubble config set security.cloud_root on",
                    err=True,
                )
                sys.exit(1)
            from .cloud import get_cloud_remote_host

            remote_host = get_cloud_remote_host(config)
        else:
            default = config.get("remote", {}).get("default_host", "")
            if default:
                from .remote import RemoteHost

                remote_host = RemoteHost.parse(default)

    # Resolve --gh-token: CLI flag > config > disabled
    if gh_token is None:
        gh_token = config.get("github", {}).get("token", False)

    if remote_host:
        if mount_specs:
            click.echo(
                "Error: --mount is not supported with remote/cloud bubbles (host paths are local)",
                err=True,
            )
            sys.exit(1)
        if base_ref and not new_branch:
            click.echo("Warning: --base has no effect without -b/--new-branch", err=True)
        _open_remote(
            remote_host,
            target,
            editor,
            no_interactive,
            network,
            custom_name,
            config,
            git_name=git_name,
            git_email=git_email,
            command=command_args,
            claude_config=claude_config,
            new_branch=new_branch,
            base_ref=base_ref,
            gh_token=gh_token,
        )
        return

    # Print security posture warnings (for auto settings)
    if not machine_readable:
        print_warnings(config)

    # Resolve claude_credentials: CLI flag > config > default (False)
    if claude_credentials is None:
        claude_credentials = config.get("claude", {}).get("credentials", False)

    # Claude Code config mounts (opt-out via --no-claude-config)
    # When security.claude_credentials=on, always include credentials
    include_creds = claude_credentials or is_enabled(config, "claude_credentials")
    cc_mounts = []
    if claude_config:
        cc_mounts = claude_config_mounts(include_credentials=include_creds)
        # Suppress auto mounts that overlap with user mounts (exact or ancestry)
        user_targets = {Path(m.target) for m in mount_specs}
        cc_mounts = [m for m in cc_mounts if not mount_overlaps(Path(m.target), user_targets)]
        # Offer to symlink ~/.bubble/claude-projects/ to ~/.claude/projects/
        if not machine_readable:
            maybe_symlink_claude_projects()

    # Editor config mounts (emacs/neovim only — suppress if user mounts overlap)
    ec_mounts = editor_config_mounts(editor)
    if ec_mounts:
        user_targets = {Path(m.target) for m in mount_specs}
        ec_mounts = [m for m in ec_mounts if not mount_overlaps(Path(m.target), user_targets)]

    # Nag about gh token if not enabled
    if not gh_token and not machine_readable:
        from .github_token import has_gh_auth

        if has_gh_auth():
            click.echo(
                "Tip: use --gh-token to inject GitHub auth into this bubble.",
                err=True,
            )

    # Local flow
    runtime = get_runtime(config)

    if not machine_readable:
        maybe_rebuild_base_image()
        maybe_rebuild_tools(runtime)
        maybe_rebuild_customize()

    # Check if target matches an existing container
    existing = find_existing_container(runtime, target)
    if existing:
        if machine_readable:
            project_dir = detect_project_dir(runtime, existing)
            machine_readable_output("reattached", existing, project_dir=project_dir)
            return
        _reattach(runtime, existing, editor, no_interactive, command=command_args)
        return

    # Parse and register target
    registry = RepoRegistry()
    try:
        t = parse_target(target, registry)
    except TargetParseError as e:
        if machine_readable:
            machine_readable_output("error", "", message=str(e))
            sys.exit(1)
        click.echo(str(e), err=True)
        sys.exit(1)
    registry.register(t.owner, t.repo)

    # Apply -b/--new-branch: override target to create a new branch
    if new_branch:
        t = Target(
            owner=t.owner,
            repo=t.repo,
            kind="branch",
            ref=new_branch,
            original=t.original,
            local_path=t.local_path,
            new_branch=True,
            base_ref=base_ref or "",
        )
    elif base_ref:
        click.echo("Warning: --base has no effect without -b/--new-branch", err=True)

    # Generate name and check for existing container with same target
    name = _generate_bubble_name(t, custom_name)
    existing = find_existing_container(
        runtime,
        target,
        generated_name=name,
        org_repo=t.org_repo,
        kind=t.kind,
        ref=t.ref,
    )
    if existing:
        if machine_readable:
            project_dir = detect_project_dir(runtime, existing)
            machine_readable_output(
                "reattached", existing, project_dir=project_dir, org_repo=t.org_repo
            )
            return
        _reattach(runtime, existing, editor, no_interactive, command=command_args)
        return

    # Resolve git source, detect language, and build image
    ensure_dirs()
    ref_path, mount_name = _resolve_ref_source(t, no_clone)
    hook, image_name = detect_and_build_image(runtime, ref_path, t, editor=editor)

    # Pre-fetch dependency bare repos for Lake pre-population
    dep_mounts = {}  # repo_name -> host_path
    if hook and is_enabled(config, "git_manifest_trust"):
        deps = hook.git_dependencies()
        if deps:
            if not machine_readable:
                click.echo("  Preparing Lake dependency mirrors...")
            for dep in deps:
                try:
                    dep_path = init_bare_repo(dep.org_repo)
                    if not ensure_rev_available(dep.org_repo, dep.rev):
                        if not machine_readable:
                            click.echo(
                                f"  Warning: rev {dep.rev[:12]} not found for {dep.name}, skipping"
                            )
                        continue
                    repo_name = dep.org_repo.split("/")[-1]
                    dep_mounts[repo_name] = dep_path
                except Exception as e:
                    if not machine_readable:
                        click.echo(f"  Warning: could not prepare {dep.name}: {e}")

    # Deduplicate and create
    existing_names = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing_names)
    if not machine_readable:
        click.echo(f"Creating bubble '{name}'...")

    # Provision, clone, and finalize
    short = repo_short_name(t.org_repo)
    try:
        provision_container(
            runtime,
            name,
            image_name,
            ref_path,
            mount_name,
            config,
            hook=hook,
            dep_mounts=dep_mounts,
            network=network,
            user_mounts=mount_specs,
            claude_mounts=cc_mounts,
            editor_mounts=ec_mounts,
        )
        checkout_branch = clone_and_checkout(runtime, name, t, mount_name, short)

        # Resolve Claude prompt: env var > auto-generate for issues
        claude_prompt = os.environ.get("BUBBLE_CLAUDE_PROMPT", "")
        if not claude_prompt and t.kind == "issue" and not machine_readable:
            from .claude import generate_issue_prompt

            click.echo(f"Fetching issue #{t.ref} for Claude prompt...")
            claude_prompt = generate_issue_prompt(t.owner, t.repo, t.ref, checkout_branch) or ""

        finalize_bubble(
            runtime,
            name,
            t,
            hook,
            image_name,
            checkout_branch,
            short,
            network,
            config,
            editor,
            no_interactive,
            machine_readable,
            git_name=git_name,
            git_email=git_email,
            command=command_args,
            claude_prompt=claude_prompt,
            gh_token=gh_token,
        )
    except Exception:
        # Clean up partially-provisioned container on failure
        if not machine_readable:
            click.echo(f"  Cleaning up failed container '{name}'...")
        try:
            runtime.delete(name, force=True)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Register commands from submodules
# ---------------------------------------------------------------------------

from .commands.lifecycle import register_lifecycle_commands  # noqa: E402
from .commands.list_cmd import register_list_command  # noqa: E402

register_list_command(main)
register_lifecycle_commands(main)


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
    """Build an image (base, lean, or lean-v4.X.Y for a specific toolchain)."""
    config = load_config()
    runtime = get_runtime(config)

    # Parse toolchain images: lean-v4.X.Y, lean-emacs-v4.X.Y, lean-neovim-v4.X.Y
    import re

    tc_match = re.fullmatch(
        r"(lean(?:-vscode|-emacs|-neovim)?)-(v\d+\.\d+\.\d+(?:-rc\d+)?)", image_name
    )
    if tc_match:
        from .images.builder import build_lean_toolchain_image

        base_lean = tc_match.group(1)
        version = tc_match.group(2)
        try:
            build_lean_toolchain_image(runtime, version, base_lean_image=base_lean)
        except Exception as e:
            click.echo(str(e), err=True)
            sys.exit(1)
    else:
        from .images.builder import build_image

        try:
            build_image(runtime, image_name)
        except ValueError as e:
            click.echo(str(e), err=True)
            sys.exit(1)


@images_group.command("delete")
@click.argument("image_name", required=False)
@click.option("--all", "delete_all", is_flag=True, help="Delete all images.")
def images_delete(image_name, delete_all):
    """Delete an image by alias or fingerprint, or --all to delete all images."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    if delete_all:
        images = runtime.list_images()
        if not images:
            click.echo("No images to delete.")
            return
        runtime.image_delete_all()
        click.echo(f"Deleted {len(images)} image(s).")
        return
    if not image_name:
        click.echo("Specify an image name or use --all.", err=True)
        sys.exit(1)
    # Try alias first, then fingerprint prefix
    if not runtime.image_exists(image_name):
        # Check if it matches a fingerprint prefix
        images = runtime.list_images()
        matches = [img for img in images if img.get("fingerprint", "").startswith(image_name)]
        if len(matches) == 1:
            fp = matches[0]["fingerprint"]
            runtime.image_delete(fp)
            click.echo(f"Deleted image '{image_name}'.")
            return
        elif len(matches) > 1:
            click.echo(
                f"Ambiguous fingerprint prefix '{image_name}' matches {len(matches)} images.",
                err=True,
            )
            sys.exit(1)
        else:
            click.echo(f"Image '{image_name}' not found.", err=True)
            sys.exit(1)
    runtime.image_delete(image_name)
    click.echo(f"Deleted image '{image_name}'.")


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
    from .container_helpers import apply_network

    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    ensure_running(runtime, name)

    apply_network(runtime, name, config)


@network_group.command("remove")
@click.argument("name")
def network_remove(name):
    """Remove network restrictions from a bubble."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    ensure_running(runtime, name)

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
    # Check if relay is explicitly locked off
    if is_locked_off(config, "relay"):
        click.echo(
            "Error: relay is locked off (security.relay=off). "
            "Re-enable: bubble config set security.relay on",
            err=True,
        )
        sys.exit(1)

    config.setdefault("relay", {})["enabled"] = True
    config.setdefault("security", {})["relay"] = "on"
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
    # Reset security.relay to auto (not off) so relay enable works as a toggle.
    # Use 'bubble config set security.relay off' to permanently lock it off.
    config.setdefault("security", {}).pop("relay", None)
    save_config(config)

    from .automation import remove_relay_daemon

    try:
        result = remove_relay_daemon()
        if result:
            click.echo(f"  Removed: {result}")
    except Exception:
        pass

    # Remove socket/port file
    from .relay import RELAY_PORT_FILE, RELAY_SOCK

    RELAY_SOCK.unlink(missing_ok=True)
    RELAY_PORT_FILE.unlink(missing_ok=True)

    click.echo("Relay disabled.")


@relay_group.command("status")
def relay_status():
    """Show relay status."""
    import platform

    config = load_config()
    enabled = is_enabled(config, "relay")
    click.echo(f"  Relay: {'enabled' if enabled else 'disabled'}")
    click.echo(f"  Security setting: {get_setting(config, 'relay')}")

    from .relay import RELAY_PORT_FILE, RELAY_SOCK

    if platform.system() == "Darwin":
        if RELAY_PORT_FILE.exists():
            port = RELAY_PORT_FILE.read_text().strip()
            click.echo(f"  Listening: TCP 127.0.0.1:{port}")
        else:
            click.echo("  Listening: not running")
    else:
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


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _save_terminal():
    """Save terminal settings so subprocess calls can't corrupt them."""
    try:
        import termios

        if sys.stdin.isatty():
            return termios.tcgetattr(sys.stdin)
    except (ImportError, termios.error):
        pass
    return None


def _restore_terminal(saved):
    """Restore terminal settings after a subprocess call."""
    if saved is not None:
        try:
            import termios

            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
        except (ImportError, termios.error):
            pass


# ---------------------------------------------------------------------------
# remote
# ---------------------------------------------------------------------------


@main.group("remote")
def remote_group():
    """Manage remote SSH host settings."""


@remote_group.command("set-default")
@click.argument("host")
def remote_set_default(host):
    """Set the default remote SSH host for new bubbles.

    HOST can be: hostname, user@hostname, or user@hostname:port
    """
    from .remote import RemoteHost

    # Validate the spec parses
    parsed = RemoteHost.parse(host)
    config = load_config()
    if "remote" not in config:
        config["remote"] = {}
    config["remote"]["default_host"] = parsed.spec_string()
    save_config(config)
    click.echo(f"Default remote host set to: {parsed.spec_string()}")


@remote_group.command("clear-default")
def remote_clear_default():
    """Clear the default remote SSH host."""
    config = load_config()
    if "remote" in config:
        config["remote"]["default_host"] = ""
        save_config(config)
    click.echo("Default remote host cleared.")


@remote_group.command("status")
def remote_status():
    """Show current remote host configuration."""
    config = load_config()
    default = config.get("remote", {}).get("default_host", "")
    if default:
        click.echo(f"Default remote host: {default}")
    else:
        click.echo("No default remote host configured.")

    # Show remote bubbles from registry
    registry = load_registry()
    remote_bubbles = [
        (name, info)
        for name, info in registry.get("bubbles", {}).items()
        if info.get("remote_host")
    ]
    if remote_bubbles:
        click.echo(f"\nRemote bubbles ({len(remote_bubbles)}):")
        for name, info in remote_bubbles:
            click.echo(f"  {name:<30} {info['remote_host']}")


# ---------------------------------------------------------------------------
# cloud
# ---------------------------------------------------------------------------


@main.group("cloud")
def cloud_group():
    """Manage Hetzner Cloud server for remote bubbles."""


@cloud_group.command("provision")
@click.option(
    "--type", "server_type", type=str, default=None, help="Server type (e.g. cx43, ccx43, cx53)"
)
@click.option("--location", type=str, default=None, help="Datacenter location (default: fsn1)")
@click.option(
    "--list", "list_types", is_flag=True, default=False, help="List available server types and exit"
)
def cloud_provision(server_type, location, list_types):
    """Provision a Hetzner Cloud server for bubble.

    Creates a server with Incus pre-installed. The server auto-shuts down
    after 15 minutes of idle (no SSH connections + low CPU) to reduce costs.
    It auto-starts again on next 'bubble --cloud <target>'.

    \b
    Common server types (default: cx43):
      cx43     8 shared vCPU, 16GB RAM (~EUR 0.02/hr)
      cx53    16 shared vCPU, 32GB RAM (~EUR 0.04/hr)
      ccx43   16 dedicated vCPU, 64GB RAM (~EUR 0.17/hr)  # needs limit increase

    Use --list to see all available server types with current pricing.
    """
    config = load_config()
    if list_types:
        from .cloud_types import list_server_types

        list_server_types(config, location=location)
        return

    if is_locked_off(config, "cloud_root"):
        click.echo(
            "Error: cloud provisioning rejected because security.cloud_root=off. "
            "Re-enable: bubble config set security.cloud_root on",
            err=True,
        )
        sys.exit(1)

    from .cloud import provision_server

    if not server_type:
        click.echo("Use --list to see all available server types.")
    provision_server(config, server_type=server_type, location=location)


@cloud_group.command("destroy")
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
def cloud_destroy(force):
    """Destroy the cloud server permanently."""
    from .cloud import destroy_server

    destroy_server(force=force)


@cloud_group.command("stop")
def cloud_stop():
    """Power off the cloud server.

    Containers are preserved on disk and will be available after restart.
    Note: Hetzner bills servers until deleted. Use 'bubble cloud destroy' to stop billing.
    """
    from .cloud import stop_server

    stop_server()


@cloud_group.command("start")
def cloud_start():
    """Power on the cloud server and wait for SSH."""
    from .cloud import start_server

    start_server()


@cloud_group.command("status")
def cloud_status():
    """Show cloud server info and status."""
    from .cloud import get_server_status

    status = get_server_status()
    if not status:
        click.echo("No cloud server provisioned.")
        click.echo("Set one up with: bubble cloud provision")
        return

    click.echo(f"  Server:   {status.get('server_name', '?')}")
    click.echo(f"  ID:       {status.get('server_id', '?')}")
    click.echo(f"  IP:       {status.get('ipv4', '?')}")
    click.echo(f"  Type:     {status.get('server_type', '?')}")
    click.echo(f"  Location: {status.get('location', '?')}")
    click.echo(f"  Status:   {status.get('status', 'unknown')}")
    if status.get("server_type_description"):
        click.echo(f"  Specs:    {status['server_type_description']}")


@cloud_group.command("ssh")
@click.argument("args", nargs=-1)
def cloud_ssh_cmd(args):
    """SSH directly to the cloud server."""
    config = load_config()
    if is_locked_off(config, "cloud_root"):
        click.echo(
            "Error: cloud SSH rejected because security.cloud_root=off. "
            "Re-enable: bubble config set security.cloud_root on",
            err=True,
        )
        sys.exit(1)
    from .cloud import cloud_ssh

    cloud_ssh(list(args) if args else None)


@cloud_group.command("default")
@click.argument("setting", required=False, type=click.Choice(["on", "off"]))
def cloud_default(setting):
    """Set whether cloud is the default for all 'bubble open'.

    When on, all bubbles go to cloud unless --local is used.
    Shows current setting if no argument given.
    """
    config = load_config()
    if setting is None:
        current = config.get("cloud", {}).get("default", False)
        state = "on" if current else "off"
        click.echo(f"Cloud default: {state}")
        if current:
            click.echo("All 'bubble open' commands use cloud. Use --local to override.")
        else:
            click.echo("Use --cloud flag or: bubble cloud default on")
        return
    config.setdefault("cloud", {})["default"] = setting == "on"
    save_config(config)
    if setting == "on":
        click.echo("Cloud set as default. All 'bubble open' will use cloud.")
        click.echo("Override with: bubble open --local <target>")
    else:
        click.echo("Cloud default disabled. Use --cloud flag for cloud bubbles.")


# ---------------------------------------------------------------------------
# skill
# ---------------------------------------------------------------------------


@main.group("skill")
def skill_group():
    """Manage the Claude Code bubble skill."""


@skill_group.command("install")
def skill_install():
    """Install the bubble skill into ~/.claude/skills/."""
    from .skill import claude_code_detected, diff_skill, install_skill, is_installed, is_up_to_date

    if not claude_code_detected():
        click.echo("~/.claude/ not found — Claude Code not detected. Skipping.")
        return

    if is_installed() and is_up_to_date():
        click.echo("Bubble skill is already installed and up to date.")
        return

    if is_installed():
        d = diff_skill()
        if d:
            click.echo("Updating bubble skill:")
            click.echo(d)

    msg = install_skill()
    click.echo(msg)


@skill_group.command("uninstall")
def skill_uninstall():
    """Remove the bubble skill from ~/.claude/skills/."""
    from .skill import uninstall_skill

    msg = uninstall_skill()
    click.echo(msg)


@skill_group.command("status")
def skill_status():
    """Check if the bubble skill is installed and up to date."""
    from .skill import claude_code_detected, is_installed, is_up_to_date

    if not claude_code_detected():
        click.echo("Claude Code not detected (~/.claude/ not found).")
        return

    if not is_installed():
        click.echo("Bubble skill is not installed.")
        click.echo("  Install with: bubble skill install")
        return

    if is_up_to_date():
        click.echo("Bubble skill is installed and up to date.")
    else:
        click.echo("Bubble skill is installed but outdated.")
        click.echo("  Update with: bubble skill install")


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------


@main.group("claude")
def claude_group():
    """Manage Claude Code settings."""


@claude_group.command("credentials")
@click.argument("setting", required=False, type=click.Choice(["on", "off"]))
def claude_credentials_cmd(setting):
    """Set whether Claude credentials are mounted into bubbles.

    When on, ~/.claude credentials (.credentials.json, .current-account)
    are mounted read-only into containers by default. Override per-bubble
    with --no-claude-credentials.

    Shows current setting if no argument given.
    """
    config = load_config()
    if setting is None:
        current = config.get("claude", {}).get("credentials", False)
        state = "on" if current else "off"
        click.echo(f"Claude credentials: {state}")
        if current:
            click.echo("Credentials are mounted into bubbles by default.")
            click.echo("Override with: bubble open --no-claude-credentials <target>")
        else:
            click.echo("Use --claude-credentials flag or: bubble claude credentials on")
        return
    config.setdefault("claude", {})["credentials"] = setting == "on"
    save_config(config)
    if setting == "on":
        click.echo("Claude credentials enabled. Mounted into all new bubbles by default.")
        click.echo("Override with: bubble open --no-claude-credentials <target>")
    else:
        click.echo("Claude credentials disabled.")


@claude_group.command("status")
def claude_status_cmd():
    """Show current Claude Code settings."""
    config = load_config()
    creds = config.get("claude", {}).get("credentials", False)
    click.echo(f"  credentials: {'on' if creds else 'off'}")


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


@main.group("tools")
def tools_group():
    """Manage tools installed in container images."""


@tools_group.command("list")
def tools_list():
    """List available tools and their current settings."""
    from .tools import available_tools

    config = load_config()
    tools_config = config.get("tools", {})

    click.echo(f"{'TOOL':<20} {'SETTING':<10}")
    click.echo("-" * 30)
    for name in available_tools():
        setting = tools_config.get(name, "auto")
        click.echo(f"{name:<20} {setting:<10}")


@tools_group.command("set")
@click.argument("tool_name")
@click.argument("value", type=click.Choice(["yes", "no", "auto"]))
def tools_set(tool_name, value):
    """Set a tool to yes, no, or auto."""
    from .tools import TOOLS

    if tool_name not in TOOLS:
        available = ", ".join(sorted(TOOLS.keys()))
        click.echo(f"Unknown tool: {tool_name}. Available: {available}", err=True)
        sys.exit(1)

    config = load_config()
    if "tools" not in config:
        config["tools"] = {}
    config["tools"][tool_name] = value
    save_config(config)
    click.echo(f"Set {tool_name} = {value}")
    click.echo("Run 'bubble images build base' to apply changes.")


@tools_group.command("status")
def tools_status():
    """Show which tools would be installed (resolved state)."""
    from .tools import TOOLS, resolve_tools, tools_hash

    config = load_config()
    enabled = resolve_tools(config)
    tools_config = config.get("tools", {})

    click.echo(f"{'TOOL':<20} {'SETTING':<10} {'RESOLVED':<10}")
    click.echo("-" * 40)
    for name in sorted(TOOLS.keys()):
        setting = tools_config.get(name, "auto")
        resolved = "install" if name in enabled else "skip"
        click.echo(f"{name:<20} {setting:<10} {resolved:<10}")

    if enabled:
        click.echo(f"\nTools hash: {tools_hash(enabled)}")
    else:
        click.echo("\nNo tools will be installed.")


@tools_group.command("update")
def tools_update():
    """Fetch latest upstream versions and update pinned versions.

    Checks nodejs.org, npmjs.org, and cli.github.com for the latest
    versions and checksums, then updates the local pins. This is a
    maintainer workflow — the updated pins should be committed and
    released so users get the new versions via package upgrade.
    """
    from .tools import fetch_latest_pins, load_pins, save_pins

    click.echo("Fetching latest versions from upstream...")
    try:
        new_pins = fetch_latest_pins()
    except Exception as e:
        click.echo(f"Error fetching upstream versions: {e}", err=True)
        sys.exit(1)

    current = load_pins()
    changes = []
    for key in sorted(new_pins):
        old = current.get(key, "(not set)")
        new = new_pins[key]
        if old != new:
            changes.append((key, old, new))

    if not changes:
        click.echo("All pins are up to date.")
        return

    click.echo(f"\n{'PIN':<25} {'CURRENT':<20} {'LATEST':<20}")
    click.echo("-" * 65)
    for key, old, new in changes:
        # Truncate checksums for display
        old_display = old[:16] + "..." if len(old) > 20 else old
        new_display = new[:16] + "..." if len(new) > 20 else new
        click.echo(f"{key:<25} {old_display:<20} {new_display:<20}")

    click.echo()
    save_pins(new_pins)
    click.echo("Pins updated. Run 'bubble images build base' to apply changes.")


@main.group("gh")
def gh_group():
    """Manage GitHub integration settings."""


@gh_group.command("token")
@click.argument("value", type=click.Choice(["on", "off"]))
def gh_token_cmd(value):
    """Enable or disable GitHub token injection into bubbles."""
    config = load_config()
    if "github" not in config:
        config["github"] = {}
    config["github"]["token"] = value == "on"
    save_config(config)
    if value == "on":
        click.echo("GitHub token injection enabled for new bubbles.")
    else:
        click.echo("GitHub token injection disabled.")


@gh_group.command("status")
def gh_status():
    """Show GitHub integration status."""
    from .github_token import has_gh_auth

    config = load_config()
    token_enabled = config.get("github", {}).get("token", False)
    host_auth = has_gh_auth()

    click.echo(f"Token injection:  {'enabled' if token_enabled else 'disabled'}")
    click.echo(f"Host gh auth:     {'authenticated' if host_auth else 'not authenticated'}")
    if not host_auth:
        click.echo("\nRun 'gh auth login' to authenticate on the host first.")
    elif not token_enabled:
        click.echo("\nRun 'bubble gh token on' to enable token injection by default.")


@main.group("config")
def config_group():
    """View and manage bubble configuration."""


@config_group.command("security")
def config_security():
    """Show current security posture."""
    config = load_config()

    click.echo(f"{'SETTING':<22} {'VALUE':<8} {'EFFECTIVE':<12} DESCRIPTION")
    click.echo("-" * 80)

    auto_count = 0
    for name, defn in SECURITY_SETTINGS.items():
        value = get_setting(config, name)
        if value == "auto":
            auto_count += 1
            effective = defn.auto_default
        else:
            effective = value
        click.echo(f"{name:<22} {value:<8} {effective:<12} {defn.description}")

    if auto_count == len(SECURITY_SETTINGS):
        click.echo(
            "\nAll settings are 'auto'. Run 'bubble config accept-risks' to silence "
            "on-by-default warnings,\nor 'bubble config lockdown' to disable "
            "off-by-default features permanently."
        )
    elif auto_count > 0:
        click.echo(f"\n{auto_count} setting(s) still 'auto'. Set explicitly to silence warnings.")


@config_group.command("set")
@click.argument("key")
@click.argument("value", type=click.Choice(SECURITY_VALID_VALUES))
def config_set(key, value):
    """Set a security setting: bubble config set security.<name> <value>."""
    # Accept both "security.X" and bare "X"
    name = key.removeprefix("security.")
    if name not in SECURITY_SETTINGS:
        available = ", ".join(sorted(SECURITY_SETTINGS.keys()))
        click.echo(f"Unknown security setting: {name}. Available: {available}", err=True)
        sys.exit(1)

    config = load_config()
    if "security" not in config:
        config["security"] = {}
    config["security"][name] = value

    # Keep relay backwards compat in sync
    if name == "relay":
        config.setdefault("relay", {})["enabled"] = value == "on"

    save_config(config)
    click.echo(f"Set security.{name} = {value}")


@config_group.command("lockdown")
def config_lockdown():
    """Set all auto-defaulting-to-off settings to off permanently."""
    config = load_config()
    if "security" not in config:
        config["security"] = {}

    changed = []
    for name, defn in SECURITY_SETTINGS.items():
        if get_setting(config, name) == "auto" and defn.auto_default == "off":
            config["security"][name] = "off"
            if name == "relay":
                config.setdefault("relay", {})["enabled"] = False
            changed.append(name)

    if changed:
        save_config(config)
        for name in changed:
            click.echo(f"  security.{name} = off")
        click.echo(f"Locked down {len(changed)} setting(s).")
    else:
        click.echo("No auto-defaulting-to-off settings to lock down.")


@config_group.command("accept-risks")
def config_accept_risks():
    """Set all auto-defaulting-to-on settings to on permanently (silences warnings)."""
    config = load_config()
    if "security" not in config:
        config["security"] = {}

    changed = []
    for name, defn in SECURITY_SETTINGS.items():
        if get_setting(config, name) == "auto" and defn.auto_default == "on":
            config["security"][name] = "on"
            changed.append(name)

    if changed:
        save_config(config)
        for name in changed:
            click.echo(f"  security.{name} = on")
        click.echo(f"Accepted {len(changed)} risk(s). On-by-default warnings silenced.")
    else:
        click.echo("No auto-defaulting-to-on settings to accept.")


@main.command()
def doctor():
    """Diagnose and fix common bubble issues."""
    import platform
    import re

    config = load_config()
    issues = 0
    fixed = 0
    saved_tty = _save_terminal()

    # 1. Check Colima (macOS only)
    if platform.system() == "Darwin":
        from .runtime.colima import is_colima_running

        if is_colima_running():
            _restore_terminal(saved_tty)
            click.echo("Colima: running")
        else:
            _restore_terminal(saved_tty)
            click.echo("Colima: not running")
            issues += 1
            if click.confirm("  Start Colima?"):
                try:
                    runtime_cfg = config.get("runtime", {})
                    from .runtime.colima import start_colima

                    start_colima(
                        cpu=runtime_cfg.get("colima_cpu", 4),
                        memory=runtime_cfg.get("colima_memory", 16),
                        disk=runtime_cfg.get("colima_disk", 60),
                        vm_type=runtime_cfg.get("colima_vm_type", "vz"),
                    )
                    _restore_terminal(saved_tty)
                    click.echo("  Started.")
                    fixed += 1
                except Exception as e:
                    click.echo(f"  Failed: {e}", err=True)

    # Get runtime (don't ensure ready — doctor should work even when things are broken)
    try:
        runtime = get_runtime(config, ensure_ready=False)
    except Exception as e:
        click.echo(f"Cannot connect to runtime: {e}", err=True)
        return

    # 2. Check for stuck incus operations
    click.echo("Checking for stuck operations...")
    try:
        result = subprocess.run(
            ["incus", "operation", "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        _restore_terminal(saved_tty)

        all_ops = json.loads(result.stdout) if result.stdout.strip() else []
        # websocket ops are active exec/console sessions (e.g. VS Code SSH), not stuck
        # Only "Running" operations can be stuck; "Success"/"Failure"/"Cancelled" are
        # just completed history that Incus retains temporarily.
        stuck = [
            op for op in all_ops if op.get("class") != "websocket" and op.get("status") == "Running"
        ]
        if stuck:
            click.echo(f"  Found {len(stuck)} stuck operation(s):")
            for op in stuck:
                desc = op.get("description", "unknown")
                click.echo(f"    {desc}")
            issues += len(stuck)
            if click.confirm("  Cancel stuck operations?"):
                cancelled = 0
                errors = []
                for op in stuck:
                    op_id = op.get("id", "")
                    if not op_id:
                        continue
                    try:
                        subprocess.run(
                            ["incus", "operation", "delete", op_id],
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=10,
                            stdin=subprocess.DEVNULL,
                        )
                        cancelled += 1
                    except subprocess.CalledProcessError as e:
                        msg = (e.stderr or "").strip()
                        errors.append(f"    {op.get('description', op_id)}: {msg}")
                    except Exception as e:
                        errors.append(f"    {op.get('description', op_id)}: {e}")
                if cancelled:
                    click.echo(f"  Cancelled {cancelled} operation(s).")
                    fixed += cancelled
                if errors:
                    click.echo("  Could not cancel some operations:", err=True)
                    for err_msg in errors:
                        click.echo(err_msg, err=True)
        else:
            click.echo("  No stuck operations.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        click.echo("  Could not check operations (incus unavailable).")

    # 3. Check registry vs actual containers
    click.echo("Checking registry consistency...")
    registry = load_registry()
    registered = set(registry.get("bubbles", {}).keys())
    containers = None
    try:
        containers = {c.name for c in runtime.list_containers(fast=True)}
    except Exception:
        click.echo("  Could not list containers (skipping consistency checks).")

    if containers is not None:
        # Stale registry entries (registered but no container)
        stale = registered - containers
        if stale:
            click.echo(f"  {len(stale)} stale registry entries (no matching container):")
            for name in sorted(stale):
                click.echo(f"    {name}")
            issues += len(stale)
            if click.confirm("  Remove stale entries?"):
                for name in stale:
                    unregister_bubble(name)
                    remove_ssh_config(name)
                click.echo(f"  Removed {len(stale)} stale entries.")
                fixed += len(stale)
        else:
            click.echo("  Registry is consistent.")

        # 4. Check SSH config for orphaned entries
        click.echo("Checking SSH config...")
        ssh_config = SSH_CONFIG_FILE
        orphaned_ssh = []
        if ssh_config.exists():
            for line in ssh_config.read_text().splitlines():
                m = re.match(r"^Host bubble-(.+)$", line.strip())
                if m:
                    bubble_name = m.group(1)
                    if bubble_name not in containers:
                        orphaned_ssh.append(bubble_name)
        if orphaned_ssh:
            click.echo(f"  {len(orphaned_ssh)} orphaned SSH config entries:")
            for name in orphaned_ssh:
                click.echo(f"    bubble-{name}")
            issues += len(orphaned_ssh)
            if click.confirm("  Remove orphaned SSH entries?"):
                for name in orphaned_ssh:
                    remove_ssh_config(name)
                click.echo(f"  Removed {len(orphaned_ssh)} entries.")
                fixed += len(orphaned_ssh)
        else:
            click.echo("  SSH config is clean.")

    # Summary
    if issues == 0:
        click.echo("\nNo issues found.")
    else:
        click.echo(f"\nFound {issues} issue(s), fixed {fixed}.")


if __name__ == "__main__":
    main()
