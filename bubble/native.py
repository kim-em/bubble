"""Non-containerized (native) workspace mode."""

import shutil
import subprocess
import sys
from pathlib import Path

import click

from .clone import _get_pr_metadata
from .config import NATIVE_DIR, ensure_dirs
from .git_store import fetch_ref, github_url, init_bare_repo
from .lifecycle import get_bubble_info, load_registry, register_bubble
from .naming import deduplicate_name, generate_name
from .output import step
from .repo_registry import RepoRegistry
from .target import TargetParseError, parse_target
from .vscode import open_editor_native


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


def _find_existing_native(name: str) -> dict | None:
    """Check if a native workspace already exists in the registry."""
    info = get_bubble_info(name)
    if info and info.get("native"):
        return info
    return None


def open_native(
    target,
    editor,
    no_interactive,
    custom_name,
    command=None,
    ephemeral=False,
):
    """Open a native (non-containerized) workspace."""
    registry = RepoRegistry()
    try:
        t = parse_target(target, registry)
    except TargetParseError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    registry.register(t.owner, t.repo)

    name = _generate_bubble_name(t, custom_name)

    # Check for existing native workspace
    existing = _find_existing_native(name)
    if existing:
        native_path = existing.get("native_path", "")
        if native_path and Path(native_path).exists():
            click.echo()
            click.echo(
                "WARNING: NATIVE MODE -- no containerization!\n"
                "This workspace runs directly on your machine with full filesystem\n"
                "and network access. Use bubble without --native for isolation."
            )
            click.echo()
            step(f"Reattaching to native workspace '{name}' at {native_path}")
            if not no_interactive:
                exit_code = open_editor_native(editor, native_path, command=command)
                if ephemeral and command:
                    from .finalization import _ephemeral_pop_and_exit

                    _ephemeral_pop_and_exit(name, exit_code)
            return

    ensure_dirs()

    # Deduplicate name against all registered bubbles (native + container)
    reg = load_registry()
    existing_names = set(reg.get("bubbles", {}).keys())
    name = deduplicate_name(name, existing_names)

    workspace_path = NATIVE_DIR / name
    if workspace_path.exists():
        click.echo(f"Native workspace directory already exists: {workspace_path}", err=True)
        sys.exit(1)

    # Print warning
    click.echo()
    click.echo(
        "WARNING: NATIVE MODE -- no containerization!\n"
        "This workspace runs directly on your machine with full filesystem\n"
        "and network access. Use bubble without --native for isolation."
    )
    click.echo()

    # Resolve reference source and clone
    url = github_url(t.org_repo)
    try:
        if t.local_path:
            ref_path = t.local_path
            step("Cloning from local path (using shared objects)...")
        else:
            ref_path = str(init_bare_repo(t.org_repo))
            if t.kind == "pr":
                step(f"Fetching PR #{t.ref}...")
                try:
                    fetch_ref(t.org_repo, f"refs/pull/{t.ref}/head:refs/pull/{t.ref}/head")
                except Exception:
                    pass
            step(f"Cloning {t.org_repo} (using shared objects)...")

        subprocess.run(
            ["git", "clone", "--reference", ref_path, url, str(workspace_path)],
            check=True,
        )

        # Checkout appropriate ref
        checkout_branch = ""
        if t.kind == "pr":
            step(f"Checking out PR #{t.ref}...")
            pr_meta = _get_pr_metadata(t.owner, t.repo, t.ref)
            pr_checkout_ok = False
            if pr_meta:
                head_ref, head_repo, clone_url = pr_meta
                is_fork = head_repo.lower() != t.org_repo.lower()
                try:
                    if is_fork:
                        fork_owner = head_repo.split("/")[0]
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(workspace_path),
                                "remote",
                                "add",
                                fork_owner,
                                clone_url,
                            ],
                            capture_output=True,
                        )
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(workspace_path),
                                "fetch",
                                fork_owner,
                                f"+refs/heads/{head_ref}:refs/remotes/{fork_owner}/{head_ref}",
                            ],
                            check=True,
                        )
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(workspace_path),
                                "checkout",
                                "-b",
                                head_ref,
                                "--track",
                                f"{fork_owner}/{head_ref}",
                            ],
                            check=True,
                        )
                    else:
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(workspace_path),
                                "fetch",
                                "origin",
                                f"+refs/heads/{head_ref}:refs/remotes/origin/{head_ref}",
                            ],
                            check=True,
                        )
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(workspace_path),
                                "checkout",
                                "-b",
                                head_ref,
                                "--track",
                                f"origin/{head_ref}",
                            ],
                            check=True,
                        )
                    checkout_branch = head_ref
                    pr_checkout_ok = True
                except subprocess.CalledProcessError:
                    pass

            if not pr_checkout_ok:
                checkout_branch = f"pr-{t.ref}"
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(workspace_path),
                        "fetch",
                        "origin",
                        f"pull/{t.ref}/head:{checkout_branch}",
                    ],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(workspace_path), "checkout", checkout_branch],
                    check=True,
                )
        elif t.kind == "branch":
            step(f"Checking out branch '{t.ref}'...")
            checkout_branch = t.ref
            if t.local_path:
                # Always fetch from local repo to pick up potentially unpushed commits.
                # Use + refspec to force-update if origin already has an older version.
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(workspace_path),
                        "fetch",
                        t.local_path,
                        f"+refs/heads/{t.ref}:refs/heads/{t.ref}",
                    ],
                    capture_output=True,
                )
            try:
                subprocess.run(
                    ["git", "-C", str(workspace_path), "switch", t.ref],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                if t.local_path:
                    # Branch wasn't on origin either; fetch and retry
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(workspace_path),
                            "fetch",
                            t.local_path,
                            f"{t.ref}:{t.ref}",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(workspace_path), "switch", t.ref],
                        check=True,
                    )
                else:
                    raise
        elif t.kind == "commit":
            step(f"Checking out commit {t.ref[:12]}...")
            subprocess.run(
                ["git", "-C", str(workspace_path), "checkout", t.ref],
                check=True,
            )

        # Get commit hash
        commit = ""
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            commit = result.stdout.strip()
        except Exception:
            pass

        register_bubble(
            name,
            t.org_repo,
            branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
            commit=commit,
            pr=int(t.ref) if t.kind == "pr" else 0,
            native=True,
            native_path=str(workspace_path),
        )
    except subprocess.CalledProcessError as e:
        if workspace_path.exists():
            shutil.rmtree(workspace_path)
        click.echo(f"Failed to create native workspace: {e}", err=True)
        sys.exit(1)

    step(f"Native workspace '{name}' created at {workspace_path}")

    if not no_interactive:
        if editor == "shell":
            step("Opening shell...")
        else:
            step("Opening VSCode...")
        exit_code = open_editor_native(editor, str(workspace_path), command=command)
        if ephemeral and command:
            from .finalization import _ephemeral_pop_and_exit

            _ephemeral_pop_and_exit(name, exit_code)
