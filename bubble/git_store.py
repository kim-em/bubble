"""Shared git bare repo management."""

import subprocess
from pathlib import Path

from .config import GIT_DIR


def bare_repo_path(org_repo: str) -> Path:
    """Get the path for a bare repo mirror. e.g. 'leanprover/lean4' → GIT_DIR/lean4.git"""
    repo_name = org_repo.split("/")[-1]
    return GIT_DIR / f"{repo_name}.git"


def github_url(org_repo: str) -> str:
    return f"https://github.com/{org_repo}.git"


def init_bare_repo(org_repo: str) -> Path:
    """Create a bare mirror repo if it doesn't exist. Returns the path."""
    path = bare_repo_path(org_repo)
    if path.exists():
        return path

    url = github_url(org_repo)
    print(f"Cloning bare mirror of {org_repo}...")
    subprocess.run(
        ["git", "clone", "--bare", url, str(path)],
        check=True,
    )
    # Configure to fetch all refs (including PRs)
    subprocess.run(
        ["git", "-C", str(path), "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"],
        check=True,
    )
    # Also fetch PR refs so we can checkout PRs
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/pull/*/head:refs/pull/*/head",
        ],
        check=True,
    )
    return path


def update_bare_repo(org_repo: str):
    """Fetch latest objects into the bare repo."""
    path = bare_repo_path(org_repo)
    if not path.exists():
        init_bare_repo(org_repo)
        return

    print(f"Updating {org_repo}...")
    subprocess.run(
        ["git", "-C", str(path), "fetch", "--all", "--prune"],
        check=True,
    )


def fetch_ref(org_repo: str, ref: str):
    """Fetch a specific ref into the bare repo (e.g. a PR ref)."""
    path = bare_repo_path(org_repo)
    if not path.exists():
        init_bare_repo(org_repo)
        return

    subprocess.run(
        ["git", "-C", str(path), "fetch", "origin", ref],
        check=True,
    )


def update_all_repos():
    """Update all bare repos found in the git store directory."""
    if not GIT_DIR.exists():
        return

    for repo_dir in sorted(GIT_DIR.iterdir()):
        if repo_dir.is_dir() and repo_dir.name.endswith(".git"):
            try:
                print(f"Updating {repo_dir.name}...")
                subprocess.run(
                    ["git", "-C", str(repo_dir), "fetch", "--all", "--prune"],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"Warning: failed to update {repo_dir.name}: {e}")


def ensure_repo(org_repo: str) -> Path:
    """Ensure a bare repo exists, creating it if needed."""
    path = bare_repo_path(org_repo)
    if not path.exists():
        init_bare_repo(org_repo)
    return path
