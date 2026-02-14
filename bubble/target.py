"""GitHub URL and target string parsing."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .repo_registry import RepoRegistry


class TargetParseError(Exception):
    """Raised when a target string cannot be parsed."""


@dataclass
class Target:
    """Parsed target specification for a bubble."""

    owner: str  # e.g. "leanprover-community"
    repo: str  # e.g. "mathlib4"
    kind: str  # "pr" | "branch" | "commit" | "repo"
    ref: str  # PR number, branch name, commit SHA, or ""
    original: str  # raw input string
    local_path: str = ""  # set when target came from a local filesystem path

    @property
    def org_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def short_name(self) -> str:
        return self.repo.lower()


def _parse_github_remote(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub remote URL.

    Handles:
      https://github.com/owner/repo.git
      https://github.com/owner/repo
      git@github.com:owner/repo.git
      git@github.com:owner/repo
    """
    # SSH format: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    # HTTPS format: https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    raise TargetParseError(
        f"Remote URL is not a GitHub repository: {url}"
    )


def _git_repo_info(path: str) -> tuple[str, str, str]:
    """Extract (owner, repo, repo_root) from a local git checkout.

    Raises TargetParseError if not a git repo or no GitHub remote.
    """
    abs_path = str(Path(path).resolve())

    # Check it's a git repo and find root
    try:
        result = subprocess.run(
            ["git", "-C", abs_path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        repo_root = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise TargetParseError(f"{abs_path} is not a git repository.")

    # Get remote URL
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        )
        remote_url = result.stdout.strip()
    except subprocess.CalledProcessError:
        raise TargetParseError(
            "No remote 'origin' found. bubble needs a GitHub remote to clone from."
        )

    if not remote_url:
        raise TargetParseError(
            "No remote 'origin' found. bubble needs a GitHub remote to clone from."
        )

    owner, repo = _parse_github_remote(remote_url)
    return owner, repo, repo_root


def _parse_local_path(raw: str) -> Target:
    """Parse a local filesystem path into a Target.

    Verifies the path is a git repo with a GitHub remote, checks for
    clean working tree and a checked-out branch. The branch does NOT
    need to be pushed — local objects are shared via --reference.
    """
    path = Path(raw).resolve()
    if not path.exists():
        raise TargetParseError(f"Path does not exist: {raw}")

    owner, repo, repo_root = _git_repo_info(str(path))

    # Get current branch
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        raise TargetParseError(
            "HEAD is detached. Check out a branch first."
        )

    if not branch:
        raise TargetParseError(
            "HEAD is detached. Check out a branch first."
        )

    # Check for dirty working tree
    result = subprocess.run(
        ["git", "-C", repo_root, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        raise TargetParseError(
            "Working tree has uncommitted changes. Commit or stash them first."
        )

    return Target(
        owner=owner, repo=repo, kind="branch", ref=branch,
        original=raw, local_path=repo_root,
    )


def parse_target(raw: str, registry: RepoRegistry) -> Target:
    """Parse a target string into a Target.

    Handles these forms:
      .                            (current directory)
      ./path  ../path  /path       (local filesystem path)
      123                          (PR number in current repo)
      https://github.com/owner/repo/pull/123
      github.com/owner/repo/pull/123
      owner/repo/pull/123
      owner/repo/tree/branch-name
      owner/repo/commit/abc123
      owner/repo
      short_name/pull/123          (uses registry)
      short_name                   (uses registry)
    """
    s = raw.strip()
    original = s

    # Local filesystem paths: start with . or /
    if s.startswith(("/", ".", "..")):
        return _parse_local_path(s)

    # Strip URL scheme
    s = re.sub(r"^https?://", "", s)

    # Strip github.com/ prefix
    s = re.sub(r"^github\.com/", "", s)

    # Strip trailing slash
    s = s.rstrip("/")

    if not s:
        raise TargetParseError(f"Empty target: {raw!r}")

    # Bare number: PR in current directory's repo
    if s.isdigit():
        try:
            owner, repo, _ = _git_repo_info(".")
            return Target(
                owner=owner, repo=repo, kind="pr", ref=s, original=original,
            )
        except TargetParseError:
            raise TargetParseError(
                f"'{s}' looks like a PR number, but the current directory "
                f"is not a git repository with a GitHub remote."
            )

    parts = s.split("/")

    # Try to match with 2+ segments (owner/repo/...)
    if len(parts) >= 4 and parts[2] == "pull":
        # owner/repo/pull/N[/...]
        owner, repo = parts[0], parts[1]
        try:
            pr_num = str(int(parts[3]))
        except ValueError:
            raise TargetParseError(f"Invalid PR number: {parts[3]!r}")
        return Target(owner=owner, repo=repo, kind="pr", ref=pr_num, original=original)

    if len(parts) >= 4 and parts[2] == "tree":
        # owner/repo/tree/branch-name (branch may contain slashes)
        owner, repo = parts[0], parts[1]
        branch = "/".join(parts[3:])
        if not branch:
            raise TargetParseError(f"Empty branch name in: {raw!r}")
        return Target(owner=owner, repo=repo, kind="branch", ref=branch, original=original)

    if len(parts) >= 4 and parts[2] == "commit":
        # owner/repo/commit/sha
        owner, repo = parts[0], parts[1]
        sha = parts[3]
        return Target(owner=owner, repo=repo, kind="commit", ref=sha, original=original)

    if len(parts) == 2:
        # owner/repo — default branch
        owner, repo = parts[0], parts[1]
        return Target(owner=owner, repo=repo, kind="repo", ref="", original=original)

    # Try short name resolution
    if len(parts) >= 3 and parts[1] == "pull":
        # short_name/pull/N
        short = parts[0]
        resolved = registry.resolve(short)
        if resolved:
            owner, repo = resolved.split("/", 1)
            try:
                pr_num = str(int(parts[2]))
            except ValueError:
                raise TargetParseError(f"Invalid PR number: {parts[2]!r}")
            return Target(owner=owner, repo=repo, kind="pr", ref=pr_num, original=original)
        if registry.is_ambiguous(short):
            options = registry.get_ambiguous_options(short)
            raise TargetParseError(
                f"'{short}' is ambiguous. Did you mean: {', '.join(options)}?"
            )
        raise TargetParseError(
            f"Unknown repo '{short}'. Use the full owner/repo form first."
        )

    if len(parts) >= 3 and parts[1] == "tree":
        # short_name/tree/branch
        short = parts[0]
        resolved = registry.resolve(short)
        if resolved:
            owner, repo = resolved.split("/", 1)
            branch = "/".join(parts[2:])
            return Target(owner=owner, repo=repo, kind="branch", ref=branch, original=original)
        if registry.is_ambiguous(short):
            options = registry.get_ambiguous_options(short)
            raise TargetParseError(
                f"'{short}' is ambiguous. Did you mean: {', '.join(options)}?"
            )
        raise TargetParseError(
            f"Unknown repo '{short}'. Use the full owner/repo form first."
        )

    if len(parts) == 1:
        # short_name — just a repo
        short = parts[0]
        resolved = registry.resolve(short)
        if resolved:
            owner, repo = resolved.split("/", 1)
            return Target(owner=owner, repo=repo, kind="repo", ref="", original=original)
        if registry.is_ambiguous(short):
            options = registry.get_ambiguous_options(short)
            raise TargetParseError(
                f"'{short}' is ambiguous. Did you mean: {', '.join(options)}?"
            )
        raise TargetParseError(
            f"Unknown repo '{short}'. Use the full owner/repo form first. "
            f"If this is a local path, use ./{short} or --path."
        )

    raise TargetParseError(
        f"Cannot parse target: {raw!r}. Use a GitHub URL or owner/repo format. "
        f"For a local path, use ./{raw} or --path."
    )
