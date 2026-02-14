"""Move or copy a local working directory into a bubble."""

import shlex
import subprocess
from pathlib import Path

from .config import repo_short_name
from .naming import deduplicate_name, generate_name
from .runtime.base import ContainerRuntime


def detect_repo_info(directory: Path) -> dict:
    """Detect git repo information from a local directory.

    Returns dict with keys: org_repo, short, branch, commit, has_changes
    """
    git_dir = directory / ".git"
    if not git_dir.exists():
        raise ValueError(f"Not a git repository: {directory}")

    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(directory)] + list(args),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # Get remote URL
    try:
        remote_url = git("remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        raise ValueError("No 'origin' remote found")

    # Parse org/repo from URL
    # Handles: https://github.com/org/repo.git, git@github.com:org/repo.git
    org_repo = remote_url
    for prefix in ["https://github.com/", "git@github.com:"]:
        if org_repo.startswith(prefix):
            org_repo = org_repo[len(prefix) :]
            break
    org_repo = org_repo.rstrip("/").removesuffix(".git")

    short = repo_short_name(org_repo)
    branch = git("branch", "--show-current") or git("rev-parse", "--short", "HEAD")
    commit = git("rev-parse", "HEAD")

    # Check for uncommitted changes
    status = git("status", "--porcelain")
    has_changes = bool(status.strip())

    return {
        "org_repo": org_repo,
        "short": short,
        "branch": branch,
        "commit": commit,
        "has_changes": has_changes,
    }


def wrap_directory(
    runtime: ContainerRuntime,
    directory: Path,
    config: dict,
    pr: int = 0,
    copy_mode: bool = False,
    custom_name: str = "",
) -> str:
    """Move or copy a local working directory into a bubble.

    Args:
        runtime: Container runtime
        directory: Local directory to wrap
        config: Loaded config
        pr: Optional PR number to associate with
        copy_mode: If True, copy state (leave local untouched). If False, move state.
        custom_name: Optional custom container name

    Returns:
        The bubble name created
    """
    info = detect_repo_info(directory)
    org_repo = info["org_repo"]
    short = info["short"]
    branch = info["branch"]
    commit = info["commit"]

    # Generate name
    if not custom_name:
        if pr:
            custom_name = generate_name(short, "pr", str(pr))
        else:
            custom_name = generate_name(short, "branch", branch)

    existing = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(custom_name, existing)

    # If move mode, stash local changes first
    stash_ref = None
    if not copy_mode and info["has_changes"]:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "stash",
                "push",
                "-m",
                f"bubble: wrapped into {name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        if "No local changes" not in result.stdout:
            stash_ref = "stash@{0}"

    # Ensure base image exists
    if not runtime.image_exists("lean-base"):
        from .images.builder import build_lean_base

        build_lean_base(runtime)

    # Launch container
    runtime.launch(name, "lean-base")

    # Wait for readiness
    import time

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
    from .git_store import ensure_repo, github_url

    bare_path = ensure_repo(org_repo)
    if bare_path.exists():
        runtime.add_disk(
            name, "shared-git", str(bare_path), f"/shared/git/{bare_path.name}", readonly=True
        )

    # Clone repo inside container
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

    # Checkout the same branch and commit
    if branch:
        q_branch = shlex.quote(branch)
        q_commit = shlex.quote(commit)
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
            # If branch doesn't exist on remote, create it at the commit
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "lean",
                    "-c",
                    f"cd /home/lean/{short} && git checkout -b {q_branch} {q_commit}",
                ],
            )

    # If there were stashed changes, create a patch and apply it in the container
    if stash_ref or (copy_mode and info["has_changes"]):
        try:
            # Generate a diff of the working tree changes
            diff_result = subprocess.run(
                ["git", "-C", str(directory), "diff"]
                + (["stash@{0}^", "stash@{0}"] if stash_ref else []),
                capture_output=True,
                text=True,
            )
            if diff_result.stdout.strip():
                # Push the diff into the container and apply
                import tempfile

                with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
                    f.write(diff_result.stdout)
                    patch_path = f.name

                subprocess.run(
                    ["incus", "file", "push", patch_path, f"{name}/tmp/wrap.patch"],
                    check=True,
                    capture_output=True,
                )
                Path(patch_path).unlink()

                runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "lean",
                        "-c",
                        f"cd /home/lean/{short} && git apply /tmp/wrap.patch && rm /tmp/wrap.patch",
                    ],
                )
        except Exception as e:
            print(f"Warning: could not transfer uncommitted changes: {e}")

    # Start SSH
    runtime.exec(name, ["bash", "-c", "service ssh start || /usr/sbin/sshd"])

    # Copy SSH keys (via file push to avoid shell injection)
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

    # Register in lifecycle
    from .lifecycle import register_bubble

    register_bubble(name, org_repo, branch=branch, commit=commit, pr=pr)

    return name
