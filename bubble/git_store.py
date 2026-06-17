"""Shared git bare repo management."""

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from .config import GIT_DIR


def bare_repo_path(org_repo: str) -> Path:
    """Get the path for a bare repo mirror. e.g. 'leanprover/lean4' → GIT_DIR/lean4.git"""
    repo_name = org_repo.split("/")[-1]
    return GIT_DIR / f"{repo_name}.git"


def github_url(org_repo: str) -> str:
    return f"https://github.com/{org_repo}.git"


def parse_github_url(url: str) -> str | None:
    """Extract org/repo from a GitHub URL. Returns None for non-GitHub URLs.

    Handles:
        https://github.com/owner/repo.git -> owner/repo
        https://github.com/owner/repo     -> owner/repo
    """
    parsed = urlparse(url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


@contextmanager
def _repo_lock(org_repo: str):
    """Acquire an exclusive file lock for a bare repo to prevent concurrent git operations."""
    GIT_DIR.mkdir(parents=True, exist_ok=True)
    repo_name = org_repo.split("/")[-1]
    lock_path = GIT_DIR / f"{repo_name}.git.lock"
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def ensure_rev_available(org_repo: str, rev: str) -> bool:
    """Ensure a specific commit is available in the bare repo.

    Returns True if the rev is (or becomes) available, False otherwise.
    """
    path = bare_repo_path(org_repo)
    if not path.exists():
        return False

    # Check if rev already exists (-- prevents option injection)
    result = subprocess.run(
        ["git", "-C", str(path), "cat-file", "-t", "--", rev],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip() == "commit":
        return True

    # Fetch under lock and check again
    with _repo_lock(org_repo):
        # Re-check after acquiring lock (another process may have fetched)
        result = subprocess.run(
            ["git", "-C", str(path), "cat-file", "-t", "--", rev],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "commit":
            return True

        try:
            subprocess.run(
                ["git", "-C", str(path), "fetch", "--all"],
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return False

    result = subprocess.run(
        ["git", "-C", str(path), "cat-file", "-t", "--", rev],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "commit"


def init_bare_repo(org_repo: str) -> Path:
    """Create a bare mirror repo if it doesn't exist. Returns the path."""
    path = bare_repo_path(org_repo)
    if path.exists():
        return path

    with _repo_lock(org_repo):
        # Re-check after acquiring lock (another process may have created it)
        if path.exists():
            return path

        url = github_url(org_repo)
        print(f"Cloning bare mirror of {org_repo}...")
        subprocess.run(
            ["git", "clone", "--bare", url, str(path)],
            check=True,
        )
        # Configure to fetch all refs (including PRs and tags)
        subprocess.run(
            ["git", "-C", str(path), "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "config",
                "--add",
                "remote.origin.fetch",
                "+refs/tags/*:refs/tags/*",
            ],
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


def fetch_ref(org_repo: str, ref: str):
    """Fetch a specific ref into the bare repo (e.g. a PR ref)."""
    path = bare_repo_path(org_repo)
    if not path.exists():
        init_bare_repo(org_repo)
        return

    with _repo_lock(org_repo):
        subprocess.run(
            ["git", "-C", str(path), "fetch", "origin", ref],
            check=True,
        )


def refresh_mirror_ref(org_repo: str, kind: str, ref: str) -> None:
    """Refresh the bare-mirror ref used for language/toolchain detection.

    Hook detection reads files (e.g. ``lean-toolchain``) straight from the
    bare mirror at the hook ref. If the repo pushed new commits since the last
    periodic ``update_all_repos`` run, that snapshot is stale, so a toolchain
    bump isn't seen and the wrong toolchain image gets selected. Refreshing the
    one relevant ref here closes that window without a full ``fetch --all``.

    PR refs are fetched separately (and freshly) by the caller, and commit
    targets are immutable, so only ``branch`` and default-branch (``repo`` /
    ``issue``) targets need refreshing. Best effort: a failed fetch (offline,
    deleted branch) leaves the existing mirror in place and emits a warning.

    For ``repo`` / ``issue`` the refreshed branch is whatever the mirror's HEAD
    points at, matching what detection reads (``git show HEAD:...``). If the
    upstream default branch changed since the mirror was cloned, the mirror's
    HEAD is itself stale; that is rare and the periodic ``update_all_repos``
    refresh corrects it.
    """
    path = bare_repo_path(org_repo)
    if not path.exists():
        return

    if kind == "branch":
        branch = ref
    elif kind in ("repo", "issue"):
        # These detect against the mirror's HEAD, i.e. its default branch.
        result = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
        branch = result.stdout.strip() if result.returncode == 0 else ""
        if not branch:
            return
    else:
        # "pr" is fetched separately by the caller; "commit" is immutable.
        return

    with _repo_lock(org_repo):
        # The "+refs/heads/<branch>:" refspec updates the stored branch ref in
        # place. The leading "+refs/heads/" guarantees the argument can't be
        # read as an option even if the branch name starts with "-".
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "fetch",
                "origin",
                f"+refs/heads/{branch}:refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        # Non-fatal, but surface it: stale detection can still pick the wrong
        # toolchain image, so the user should know the mirror wasn't refreshed.
        from .output import detail

        detail(f"Warning: could not refresh '{branch}' from {org_repo}; using cached mirror.")


def update_all_repos():
    """Update all bare repos found in the git store directory."""
    if not GIT_DIR.exists():
        return

    for repo_dir in sorted(GIT_DIR.iterdir()):
        if repo_dir.is_dir() and repo_dir.name.endswith(".git"):
            # Derive org_repo name for locking (just need the repo part)
            repo_name = repo_dir.name[:-4]  # strip .git
            try:
                with _repo_lock(f"_/{repo_name}"):
                    print(f"Updating {repo_dir.name}...")
                    subprocess.run(
                        ["git", "-C", str(repo_dir), "fetch", "--all", "--prune"],
                        check=True,
                    )
            except subprocess.CalledProcessError as e:
                print(f"Warning: failed to update {repo_dir.name}: {e}")


def repo_is_known(org_repo: str) -> bool:
    """Check if a bare repo exists in the git store."""
    return bare_repo_path(org_repo).exists()
