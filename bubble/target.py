"""GitHub URL and target string parsing."""

import re
from dataclasses import dataclass

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

    @property
    def org_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def short_name(self) -> str:
        return self.repo.lower()


def parse_target(raw: str, registry: RepoRegistry) -> Target:
    """Parse a target string into a Target.

    Handles these forms:
      https://github.com/owner/repo/pull/123
      github.com/owner/repo/pull/123
      owner/repo/pull/123
      owner/repo/tree/branch-name
      owner/repo/commit/abc123
      owner/repo
      short_name/pull/123      (uses registry)
      short_name               (uses registry)
    """
    s = raw.strip()
    original = s

    # Strip URL scheme
    s = re.sub(r"^https?://", "", s)

    # Strip github.com/ prefix
    s = re.sub(r"^github\.com/", "", s)

    # Strip trailing slash
    s = s.rstrip("/")

    if not s:
        raise TargetParseError(f"Empty target: {raw!r}")

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
            f"Unknown repo '{short}'. Use the full owner/repo form first."
        )

    raise TargetParseError(
        f"Cannot parse target: {raw!r}. Use a GitHub URL or owner/repo format."
    )
