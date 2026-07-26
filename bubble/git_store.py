"""Shared git bare repo management."""

import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from .config import GIT_DIR, HOST_DATA_DIR, LEGACY_GIT_DIR

GIT_LOCK_DIR = HOST_DATA_DIR / "locks" / "git"
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def canonical_repo(org_repo: str) -> str:
    """Validate and normalize an owner/repository identifier."""
    parts = org_repo.split("/")
    if len(parts) != 2 or any(
        not part or part in (".", "..") or not _COMPONENT_RE.fullmatch(part) for part in parts
    ):
        raise ValueError(f"Invalid GitHub repository: {org_repo!r}")
    repo = parts[1].removesuffix(".git")
    if not repo or repo in (".", "..") or not _COMPONENT_RE.fullmatch(repo):
        raise ValueError(f"Invalid GitHub repository: {org_repo!r}")
    return f"{parts[0].lower()}/{repo.lower()}"


def bare_repo_path(org_repo: str) -> Path:
    """Return the nested host-global path for a GitHub bare mirror."""
    owner, repo = canonical_repo(org_repo).split("/", 1)
    return GIT_DIR / owner / f"{repo}.git"


def github_url(org_repo: str) -> str:
    return f"https://github.com/{canonical_repo(org_repo)}.git"


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
    if len(parts) == 2:
        try:
            return canonical_repo(f"{parts[0]}/{parts[1]}")
        except ValueError:
            return None
    return None


@contextmanager
def _repo_lock(org_repo: str):
    """Acquire an exclusive file lock for a bare repo to prevent concurrent git operations."""
    canonical = canonical_repo(org_repo)
    GIT_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    lock_path = GIT_LOCK_DIR / f"{lock_name}.lock"
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _origin_repo(path: Path) -> str | None:
    if not (path / "HEAD").is_file():
        return None
    result = subprocess.run(
        ["git", "--git-dir", str(path), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    return parse_github_url(result.stdout.strip()) if result.returncode == 0 else None


def _legacy_candidates(org_repo: str):
    repo_name = canonical_repo(org_repo).split("/", 1)[1]
    seen = set()
    for parent in (LEGACY_GIT_DIR, HOST_DATA_DIR / "git"):
        if not parent.is_dir():
            continue
        for candidate in parent.iterdir():
            if candidate in seen or candidate.is_symlink() or not candidate.is_dir():
                continue
            seen.add(candidate)
            if candidate.name.lower() == f"{repo_name}.git":
                yield candidate


def find_existing_mirror(org_repo: str) -> Path | None:
    """Return a nested mirror, or an origin-verified legacy mirror."""
    destination = bare_repo_path(org_repo)
    if destination.is_dir():
        return destination
    canonical = canonical_repo(org_repo)
    for candidate in _legacy_candidates(org_repo):
        if _origin_repo(candidate) == canonical:
            return candidate
    return None


def _migrate_legacy_mirror(org_repo: str, destination: Path) -> bool:
    """Atomically adopt an unambiguous legacy flat mirror."""
    for candidate in _legacy_candidates(org_repo):
        if _origin_repo(candidate) != canonical_repo(org_repo):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(candidate, destination)
        except OSError:
            # A custom BUBBLE_HOME can live on another filesystem. Keep its
            # mirror intact and let the caller atomically clone a fresh copy.
            continue
        _configure_mirror(destination)
        try:
            candidate.symlink_to(destination, target_is_directory=True)
        except OSError:
            pass
        return True
    return False


def _configure_mirror(path: Path) -> None:
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

        if _migrate_legacy_mirror(org_repo, path):
            return path

        url = github_url(org_repo)
        print(f"Cloning bare mirror of {org_repo}...")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".bubble-clone-", dir=path.parent) as temp_dir:
            temporary = Path(temp_dir) / path.name
            subprocess.run(["git", "clone", "--bare", url, str(temporary)], check=True)
            _configure_mirror(temporary)
            os.replace(temporary, path)
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
    # Opportunistically finish the flat-layout migration even for repositories
    # not opened interactively after an upgrade.
    legacy_repos = []
    for parent in (LEGACY_GIT_DIR, HOST_DATA_DIR / "git"):
        if not parent.is_dir():
            continue
        for candidate in parent.iterdir():
            org_repo = _origin_repo(candidate) if candidate.is_dir() else None
            if org_repo:
                legacy_repos.append(org_repo)
    for org_repo in sorted(set(legacy_repos)):
        destination = bare_repo_path(org_repo)
        if destination.exists():
            continue
        with _repo_lock(org_repo):
            if not destination.exists():
                _migrate_legacy_mirror(org_repo, destination)

    if not GIT_DIR.exists():
        return

    for repo_dir in sorted(GIT_DIR.glob("*/*.git")):
        org_repo = _origin_repo(repo_dir)
        if not org_repo:
            print(f"Warning: skipping mirror with invalid origin: {repo_dir}")
            continue
        try:
            with _repo_lock(org_repo):
                print(f"Updating {org_repo}...")
                subprocess.run(
                    ["git", "-C", str(repo_dir), "fetch", "--all", "--prune"],
                    check=True,
                )
        except subprocess.CalledProcessError as e:
            print(f"Warning: failed to update {org_repo}: {e}")


def repo_is_known(org_repo: str) -> bool:
    """Check if a bare repo exists in the git store."""
    return find_existing_mirror(org_repo) is not None
