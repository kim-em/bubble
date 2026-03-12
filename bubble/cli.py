"""CLI entry point for bubble."""

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
    codex_config_mounts,
    editor_config_mounts,
    ensure_dirs,
    is_first_run,
    load_config,
    maybe_symlink_claude_projects,
    parse_mounts,
    repo_short_name,
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
)
from .image_management import (
    detect_and_build_image,
    maybe_rebuild_base_image,
    maybe_rebuild_customize,
    maybe_rebuild_tools,
)
from .lifecycle import register_bubble
from .naming import deduplicate_name, generate_name
from .native import open_native
from .provisioning import mount_overlaps, provision_container
from .repo_registry import RepoRegistry
from .security import (
    is_enabled,
    is_locked_off,
    print_warnings,
    should_include_credentials,
)
from .setup import get_runtime
from .target import Target, TargetParseError, parse_target
from .vscode import (
    add_ssh_config,
    open_editor,
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


BASIC_COMMANDS = {"list", "pause", "pop"}


class BubbleGroup(click.Group):
    """Custom group that routes unknown first args to the implicit 'open' command."""

    def format_usage(self, ctx, formatter):
        formatter.write("Usage: bubble TARGET [TARGET...] [OPTIONS]\n")
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

        with formatter.section("Common target options"):
            formatter.write_dl(
                [
                    ("--shell", "Drop into SSH session instead of VSCode"),
                    ("--ssh HOST", "Run on a remote host"),
                    ("--cloud", "Run on auto-provisioned Hetzner Cloud server"),
                    ("-b NAME", "Create a new branch"),
                    ("--mount PATH", "Mount a host directory into the container"),
                    ("--native", "Non-containerized workspace (local clone)"),
                ]
            )
        formatter.write_paragraph()
        formatter.write("  For all target options run: bubble open --help\n")

    def parse_args(self, ctx, args):
        """If no known command is found as the first positional arg, prepend 'open'.

        This supports both `bubble TARGET` and `bubble --ssh HOST TARGET`.
        Only the first non-option token is checked, so that targets like
        `list` or `pause` in later positions don't hijack routing.
        Also handles `bubble -b branch_name` (options only, no target).
        """
        first_positional = next((a for a in args if not a.startswith("-")), None)
        has_branch_flag = "-b" in args or "--new-branch" in args
        if args and (
            (first_positional is not None and first_positional not in self.commands)
            or has_branch_flag
        ):
            args = ["open"] + args
        return super().parse_args(ctx, args)


@click.group(cls=BubbleGroup, context_settings=dict(help_option_names=["-h", "--help"]))
@click.version_option(__version__)
@click.pass_context
def main(ctx):
    """bubble: Open a containerized dev environment.

    Run bubble TARGET to create (or reattach to) an isolated container and
    open it in VSCode via Remote SSH. Use --shell for a plain SSH session.
    Multiple targets can be specified to open several bubbles at once.

    \b
    Examples:
      bubble .                                      Current directory
      bubble leanprover-community/mathlib4          GitHub repo
      bubble https://github.com/owner/repo/pull/42  Pull request
      bubble mathlib4/pull/123                      PR shorthand
      bubble 456                                    PR in current repo
      bubble 12 13 14                               Multiple targets
    """
    ctx.ensure_object(dict)
    # Record first-run status before any load_config() creates the file
    ctx.obj["first_run"] = is_first_run()


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
                click.echo(
                    f"Repo '{t.org_repo}' has not been cloned yet. "
                    f"Run without --no-clone to fetch it automatically.",
                    err=True,
                )
                sys.exit(1)
        else:
            bare_path = init_bare_repo(t.org_repo)
        ref_path = bare_path
        mount_name = ref_path.name

        if t.kind == "pr":
            from .output import step

            step(f"Fetching PR #{t.ref}...")
            try:
                fetch_ref(t.org_repo, f"refs/pull/{t.ref}/head:refs/pull/{t.ref}/head")
            except subprocess.CalledProcessError:
                click.echo("  Warning: could not prefetch PR ref; continuing with normal clone.")

    return ref_path, mount_name


def _resolve_claude_prompt_locally(target: str, new_branch: str | None = None) -> str:
    """Resolve a Claude prompt on the local machine for remote bubbles.

    Checks BUBBLE_CLAUDE_PROMPT env var first, then auto-generates for issue
    and PR targets using the local gh CLI (which may not exist on remote hosts).
    """
    prompt = os.environ.get("BUBBLE_CLAUDE_PROMPT", "")
    if prompt:
        return prompt

    # Try to parse the target to detect issue/PR targets
    try:
        from .repo_registry import RepoRegistry
        from .target import parse_target

        t = parse_target(target, RepoRegistry())
        if t.kind == "issue":
            from .claude import generate_issue_prompt

            branch = new_branch or f"issue-{t.ref}"
            from .output import detail

            detail(f"Fetching issue #{t.ref} for Claude prompt...")
            prompt = generate_issue_prompt(t.owner, t.repo, t.ref, branch) or ""
        elif t.kind == "pr":
            from .claude import generate_pr_prompt

            branch = new_branch or f"pr-{t.ref}"
            from .output import detail

            detail(f"Fetching PR #{t.ref} for Claude prompt...")
            prompt = generate_pr_prompt(t.owner, t.repo, t.ref, branch) or ""
    except Exception:
        pass

    return prompt


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
    claude_credentials=None,
    codex_credentials=None,
    new_branch=None,
    base_ref=None,
):
    """Open a bubble on a remote host, then connect locally."""
    from .remote import remote_open

    # Resolve Claude prompt locally (gh CLI may not exist on the remote)
    claude_prompt = _resolve_claude_prompt_locally(target, new_branch=new_branch)

    try:
        result = remote_open(
            remote_host,
            target,
            network=network,
            custom_name=custom_name,
            git_name=git_name,
            git_email=git_email,
            claude_config=claude_config,
            claude_credentials=claude_credentials,
            codex_credentials=codex_credentials,
            new_branch=new_branch,
            base_ref=base_ref,
            claude_prompt=claude_prompt,
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

    # Set up GitHub auth via tunneled auth proxy
    if is_enabled(config, "github_auth"):
        from .github_token import setup_gh_token
        from .tools import resolve_tools

        owner, repo = "", ""
        if org_repo and "/" in org_repo:
            owner, repo = org_repo.split("/", 1)
        gh_enabled = "gh" in resolve_tools(config)
        setup_gh_token(
            None,
            name,
            owner=owner,
            repo=repo,
            remote_host=remote_host,
            gh_enabled=gh_enabled,
            config=config,
        )

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
        project_dir=project_dir,
    )

    from .output import detail, step

    step(f"Bubble '{name}' ready on {remote_host.ssh_destination}.")
    detail(f"SSH: ssh bubble-{name}")

    if not no_interactive:
        echo_editor_opening(editor)
        open_editor(editor, name, project_dir, workspace_file=workspace_file, command=command)


def _reattach(runtime, name, editor, no_interactive, command=None):
    """Re-attach to an existing container."""
    ensure_running(runtime, name)

    if no_interactive:
        from .output import step

        step(f"Bubble '{name}' is running.")
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
                    from .output import step

                    step("Working tree is clean, pulling latest...")
                    try:
                        runtime.exec(
                            name,
                            ["su", "-", "user", "-c", f"cd {q_dir} && git pull --ff-only"],
                        )
                    except RuntimeError:
                        click.echo(
                            "  Warning: could not pull latest; continuing with current state."
                        )
        except RuntimeError:
            pass  # Can't check status, skip pull

    echo_editor_opening(editor)
    open_editor(editor, name, project_dir, command=command)


# The "open" command is hidden from help because users invoke it implicitly via
# `bubble TARGET`. It exists as an explicit subcommand because remote.py calls
# `bubble open --no-interactive --machine-readable` on remote hosts.
@main.command("open", hidden=True)
@click.argument("targets", nargs=-1)
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
    help="Mount ~/.claude credentials into container (default: from config or enabled)",
)
@click.option(
    "--codex-credentials/--no-codex-credentials",
    default=None,
    help="Mount ~/.codex credentials into container (default: from config or enabled)",
)
@click.option(
    "--claude-prompt-stdin",
    is_flag=True,
    hidden=True,
    help="Read Claude prompt from stdin (used internally by remote open).",
)
def open_cmd(
    targets,
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
    codex_credentials,
    claude_prompt_stdin,
):
    """Open a bubble for one or more targets (GitHub URL, repo, local path, or PR number)."""
    # When -b is used without an explicit target, infer owner/repo from cwd
    if not targets:
        if new_branch:
            from .target import _git_repo_info

            try:
                owner, repo, _ = _git_repo_info(".")
                targets = (f"{owner}/{repo}",)
            except TargetParseError as e:
                click.echo(str(e), err=True)
                sys.exit(1)
        else:
            click.echo("Error: missing target. Usage: bubble TARGET [OPTIONS]", err=True)
            sys.exit(1)

    # Reject options that are ambiguous with multiple targets
    if len(targets) > 1:
        if custom_name:
            click.echo("Error: --name cannot be used with multiple targets", err=True)
            sys.exit(1)
        if new_branch:
            click.echo("Error: -b/--new-branch cannot be used with multiple targets", err=True)
            sys.exit(1)
        if machine_readable:
            click.echo("Error: --machine-readable cannot be used with multiple targets", err=True)
            sys.exit(1)

    multi = len(targets) > 1
    errors = []
    for target in targets:
        try:
            _open_single(
                target,
                editor_choice=editor_choice,
                shell=shell,
                emacs=emacs,
                neovim=neovim,
                ssh_host=ssh_host,
                cloud=cloud,
                force_local=force_local,
                no_interactive=no_interactive,
                machine_readable=machine_readable,
                network=network,
                custom_name=custom_name,
                command=command,
                native=native,
                force_path=force_path,
                new_branch=new_branch,
                base_ref=base_ref,
                no_clone=no_clone,
                git_name=git_name,
                git_email=git_email,
                mounts=mounts,
                claude_config=claude_config,
                claude_credentials=claude_credentials,
                codex_credentials=codex_credentials,
                claude_prompt_stdin=claude_prompt_stdin,
            )
        except SystemExit as e:
            if not multi:
                raise
            if e.code:  # Only treat nonzero exits as failures
                errors.append(target)
        except TargetParseError as e:
            if not multi:
                raise
            click.echo(
                f"Error processing target '{target}': {e}\n"
                f"  Supported formats: GitHub URL, owner/repo, local path, "
                f"PR/issue number, or short name.",
                err=True,
            )
            errors.append(target)
        except Exception as e:
            if not multi:
                raise
            click.echo(f"Error processing target '{target}': {e}", err=True)
            errors.append(target)

    if errors:
        click.echo(f"\nFailed targets: {', '.join(errors)}", err=True)
        sys.exit(1)


def _open_single(
    target,
    *,
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
    codex_credentials,
    claude_prompt_stdin,
):
    """Open a single bubble target."""
    if force_path and not target.startswith(("/", ".", "..")):
        target = "./" + target

    config = load_config()

    # Enforce security: reject all user mounts when user_mounts is locked off
    # This covers both --mount CLI flags and [[mounts]] from config
    user_mounts_locked = is_locked_off(config, "user_mounts")
    if user_mounts_locked and (mounts or config.get("mounts")):
        click.echo(
            "Error: user mounts rejected because security.user-mounts=off. "
            "Re-enable: bubble security set user-mounts on",
            err=True,
        )
        sys.exit(1)

    # Enforce security: reject --claude-credentials when locked off
    if claude_credentials and is_locked_off(config, "claude_credentials"):
        click.echo(
            "Error: --claude-credentials rejected because security.claude-credentials=off. "
            "Re-enable: bubble security set claude-credentials on",
            err=True,
        )
        sys.exit(1)

    # Enforce security: reject --codex-credentials when locked off
    if codex_credentials and is_locked_off(config, "codex_credentials"):
        click.echo(
            "Error: --codex-credentials rejected because security.codex-credentials=off. "
            "Re-enable: bubble security set codex-credentials on",
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
    from .notices import Notices

    notices = Notices()
    if not machine_readable:
        from .notices import maybe_print_welcome

        maybe_print_welcome(notices=notices)
        print_warnings(config, notices=notices)

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
        if not machine_readable:
            notices.finish()
        open_native(target, editor, no_interactive, custom_name, command=command_args)
        return

    # Priority: --local > --ssh > --cloud > [cloud] default > [remote] default_host
    remote_host = None
    if not force_local and not machine_readable:
        if ssh_host:
            from .remote import RemoteHost

            remote_host = RemoteHost.parse(ssh_host)
        elif cloud or config.get("cloud", {}).get("default", False):
            from .cloud import get_cloud_remote_host

            remote_host = get_cloud_remote_host(config)
        else:
            default = config.get("remote", {}).get("default_host", "")
            if default:
                from .remote import RemoteHost

                remote_host = RemoteHost.parse(default)

    # Resolve claude_credentials: CLI flag > config > default (True)
    if claude_credentials is None:
        claude_credentials = config.get("claude", {}).get("credentials", True)

    # Resolve codex_credentials: CLI flag > config > default (True)
    if codex_credentials is None:
        codex_credentials = config.get("codex", {}).get("credentials", True)

    if remote_host:
        if mount_specs:
            click.echo(
                "Error: --mount is not supported with remote/cloud bubbles (host paths are local)",
                err=True,
            )
            sys.exit(1)
        if base_ref and not new_branch:
            click.echo("Warning: --base has no effect without -b/--new-branch", err=True)
        if not machine_readable:
            notices.finish()
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
            claude_credentials=claude_credentials,
            codex_credentials=codex_credentials,
            new_branch=new_branch,
            base_ref=base_ref,
        )
        return

    # Pre-compute user mount targets for overlap checking below
    user_targets = {Path(m.target) for m in mount_specs}

    # Claude Code config mounts (opt-out via --no-claude-config)
    include_creds = should_include_credentials(claude_credentials, config, "claude_credentials")
    cc_mounts = []
    if claude_config:
        cc_mounts = claude_config_mounts(include_credentials=include_creds)
        # Suppress auto mounts that overlap with user mounts (exact or ancestry)
        cc_mounts = [m for m in cc_mounts if not mount_overlaps(Path(m.target), user_targets)]
        # Hint about symlinking ~/.bubble/claude-projects/ to ~/.claude/projects/
        if not machine_readable:
            maybe_symlink_claude_projects(config, notices=notices)

    # Codex config mounts
    include_codex_creds = should_include_credentials(codex_credentials, config, "codex_credentials")
    cx_mounts = codex_config_mounts(include_credentials=include_codex_creds)
    if cx_mounts:
        cx_mounts = [m for m in cx_mounts if not mount_overlaps(Path(m.target), user_targets)]

    # Editor config mounts (emacs/neovim only — suppress if user mounts overlap)
    ec_mounts = editor_config_mounts(editor)
    if ec_mounts:
        ec_mounts = [m for m in ec_mounts if not mount_overlaps(Path(m.target), user_targets)]

    # Local flow
    runtime = get_runtime(config)

    if not machine_readable:
        maybe_rebuild_base_image()
        maybe_rebuild_tools(runtime, notices=notices)
        maybe_rebuild_customize(notices=notices)

    # Check if target matches an existing container
    existing = find_existing_container(runtime, target)
    if existing:
        if machine_readable:
            project_dir = detect_project_dir(runtime, existing)
            machine_readable_output("reattached", existing, project_dir=project_dir)
            return
        notices.finish()
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
        notices.finish()
        _reattach(runtime, existing, editor, no_interactive, command=command_args)
        return

    # Resolve git source, detect language, and build image
    if not machine_readable:
        notices.finish()
    ensure_dirs()
    ref_path, mount_name = _resolve_ref_source(t, no_clone)
    hook, image_name = detect_and_build_image(runtime, ref_path, t)

    # Pre-fetch dependency bare repos for Lake pre-population
    dep_mounts = {}  # repo_name -> host_path
    if hook and is_enabled(config, "git_manifest_trust"):
        deps = hook.git_dependencies()
        if deps:
            if not machine_readable:
                from .output import detail

                detail("Preparing Lake dependency mirrors...")
            for dep in deps:
                try:
                    dep_path = init_bare_repo(dep.org_repo)
                    if not ensure_rev_available(dep.org_repo, dep.rev):
                        if not machine_readable:
                            detail(
                                f"Warning: rev {dep.rev[:12]} not found for {dep.name}, skipping"
                            )
                        continue
                    repo_name = dep.org_repo.split("/")[-1]
                    dep_mounts[repo_name] = dep_path
                except Exception as e:
                    if not machine_readable:
                        detail(f"Warning: could not prepare {dep.name}: {e}")

    # Deduplicate and create
    existing_names = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing_names)
    if not machine_readable:
        from .output import step

        step(f"Creating bubble '{name}'...")

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
            codex_mounts=cx_mounts,
            editor_mounts=ec_mounts,
        )
        checkout_branch = clone_and_checkout(runtime, name, t, mount_name, short)

        # Resolve Claude prompt: stdin flag > env var > auto-generate for issues/PRs
        # The stdin flag is set by _open_remote() which generates the prompt locally.
        claude_prompt = ""
        if claude_prompt_stdin:
            claude_prompt = sys.stdin.read()
        if not claude_prompt:
            claude_prompt = os.environ.get("BUBBLE_CLAUDE_PROMPT", "")
        if not claude_prompt and t.kind == "issue" and not machine_readable:
            from .claude import generate_issue_prompt
            from .output import detail

            detail(f"Fetching issue #{t.ref} for Claude prompt...")
            claude_prompt = generate_issue_prompt(t.owner, t.repo, t.ref, checkout_branch) or ""
        elif not claude_prompt and t.kind == "pr" and not machine_readable:
            from .claude import generate_pr_prompt
            from .output import detail

            detail(f"Fetching PR #{t.ref} for Claude prompt...")
            claude_prompt = generate_pr_prompt(t.owner, t.repo, t.ref, checkout_branch) or ""

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
        )
    except Exception:
        # Clean up partially-provisioned container on failure
        if not machine_readable:
            from .output import detail

            detail(f"Cleaning up failed container '{name}'...")
        try:
            runtime.delete(name, force=True)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Register commands from submodules
# ---------------------------------------------------------------------------

from .commands.cloud_cmd import register_cloud_commands  # noqa: E402
from .commands.doctor import register_doctor_command  # noqa: E402
from .commands.images import register_images_commands  # noqa: E402
from .commands.infrastructure import register_infrastructure_commands  # noqa: E402
from .commands.lifecycle import register_lifecycle_commands  # noqa: E402
from .commands.list_cmd import register_list_command  # noqa: E402
from .commands.relay_cmd import register_relay_commands  # noqa: E402
from .commands.remote_cmd import register_remote_commands  # noqa: E402
from .commands.security_cmd import register_security_commands  # noqa: E402
from .commands.settings import register_settings_commands  # noqa: E402

register_list_command(main)
register_lifecycle_commands(main)
register_images_commands(main)
register_infrastructure_commands(main)
register_relay_commands(main)
register_remote_commands(main)
register_cloud_commands(main)
register_security_commands(main)
register_settings_commands(main)
register_doctor_command(main)


if __name__ == "__main__":
    main()
