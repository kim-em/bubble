"""Container lifecycle management: archive, reconstitute, registry tracking."""

import json
import shlex
import time
from datetime import datetime, timezone

from .config import REGISTRY_FILE, repo_short_name
from .git_store import ensure_repo, github_url
from .runtime.base import ContainerRuntime


def _load_registry() -> dict:
    """Load the bubble registry."""
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return {"bubbles": {}}


def _save_registry(registry: dict):
    """Save the bubble registry."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2) + "\n")


def register_bubble(
    name: str,
    org_repo: str,
    branch: str = "",
    commit: str = "",
    pr: int = 0,
    base_image: str = "lean-base",
):
    """Record a bubble's creation in the registry."""
    registry = _load_registry()
    registry["bubbles"][name] = {
        "org_repo": org_repo,
        "branch": branch,
        "commit": commit,
        "pr": pr,
        "base_image": base_image,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "state": "active",
    }
    _save_registry(registry)


def get_bubble_info(name: str) -> dict | None:
    """Get registry info for a bubble."""
    registry = _load_registry()
    return registry["bubbles"].get(name)


def check_git_synced(runtime: ContainerRuntime, name: str, project_dir: str) -> tuple[bool, str]:
    """Check if a bubble's git state is fully synced to remotes.

    Returns (is_synced, reason_if_not).
    """
    try:
        q_dir = shlex.quote(project_dir)
        # Check for uncommitted changes
        status = runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd {q_dir} && git status --porcelain",
            ],
        )
        if status.strip():
            return False, f"Uncommitted changes:\n{status.strip()}"

        # Check for unpushed commits on all local branches
        unpushed = runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd {q_dir} && git log --branches --not --remotes --oneline",
            ],
        )
        if unpushed.strip():
            return False, f"Unpushed commits:\n{unpushed.strip()}"

        return True, ""
    except Exception as e:
        return False, f"Error checking git state: {e}"


def archive_bubble(runtime: ContainerRuntime, name: str, project_dir: str | None = None) -> dict:
    """Archive a bubble: save state metadata, destroy container.

    Returns the archived state dict.
    """
    registry = _load_registry()
    info = registry["bubbles"].get(name, {})

    if not project_dir:
        short = repo_short_name(info.get("org_repo", ""))
        project_dir = f"/home/lean/{short}" if short else "/home/lean"

    # Gather current git state
    q_dir = shlex.quote(project_dir)
    try:
        branch = runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd {q_dir} && git branch --show-current",
            ],
        ).strip()
    except Exception:
        branch = info.get("branch", "")

    try:
        commit = runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd {q_dir} && git rev-parse HEAD",
            ],
        ).strip()
    except Exception:
        commit = info.get("commit", "")

    try:
        toolchain = runtime.exec(
            name,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cat {q_dir}/lean-toolchain",
            ],
        ).strip()
    except Exception:
        toolchain = ""

    # Build archived state
    archived_state = {
        "org_repo": info.get("org_repo", ""),
        "branch": branch,
        "commit": commit,
        "pr": info.get("pr", 0),
        "base_image": info.get("base_image", "lean-base"),
        "toolchain": toolchain,
        "created_at": info.get("created_at", ""),
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "state": "archived",
    }

    # Update registry
    registry["bubbles"][name] = archived_state
    _save_registry(registry)

    # Destroy container
    runtime.delete(name, force=True)

    return archived_state


def reconstitute_bubble(runtime: ContainerRuntime, name: str, state: dict | None = None) -> str:
    """Reconstitute an archived bubble from saved state.

    Returns the new container name (may differ if original name is taken).
    """
    if state is None:
        registry = _load_registry()
        state = registry["bubbles"].get(name)
        if not state:
            raise ValueError(f"No archived state found for '{name}'")

    org_repo = state["org_repo"]
    short = repo_short_name(org_repo)
    base_image = state.get("base_image", "lean-base")
    branch = state.get("branch", "")
    commit = state.get("commit", "")

    # Ensure base image exists
    if not runtime.image_exists(base_image):
        if not runtime.image_exists("lean-base"):
            from .images.builder import build_lean_base

            build_lean_base(runtime)
        base_image = "lean-base"

    # Launch container
    runtime.launch(name, base_image)

    # Wait for readiness
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

    # Clone repo
    url = github_url(org_repo)
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

    # Checkout branch and verify commit
    if branch:
        q_branch = shlex.quote(branch)
        try:
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
        except Exception:
            # Branch might not exist on remote, try fetching PR
            pr = state.get("pr", 0)
            if pr:
                runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "lean",
                        "-c",
                        f"cd /home/lean/{short}"
                        f" && git fetch origin pull/{pr}/head:{q_branch}"
                        f" && git checkout {q_branch}",
                    ],
                )

    if commit:
        q_commit = shlex.quote(commit)
        # Verify commit is reachable
        try:
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "lean",
                    "-c",
                    f"cd /home/lean/{short} && git merge-base --is-ancestor {q_commit} HEAD",
                ],
            )
        except Exception:
            print(f"Warning: commit {commit[:12]} is not an ancestor of current HEAD")
            # Reset to the exact commit anyway
            try:
                runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "lean",
                        "-c",
                        f"cd /home/lean/{short} && git checkout {q_commit}",
                    ],
                )
            except Exception:
                print(f"Warning: could not checkout commit {commit[:12]}")

    # Start SSH
    runtime.exec(name, ["bash", "-c", "service ssh start || /usr/sbin/sshd"])

    # Update registry
    registry = _load_registry()
    registry["bubbles"][name] = {
        **state,
        "state": "active",
        "reconstituted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(registry)

    return name
