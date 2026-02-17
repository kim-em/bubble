"""Container cleanness checking.

A container is "clean" (safe to discard without data loss) when:
1. No non-hidden files/dirs in /home/user except the project directory
2. Git working tree is clean (no modified/staged/untracked files)
3. No git stashes
4. No unpushed commits (accounting for PR checkout branches)
"""

import shlex
from dataclasses import dataclass, field

from .lifecycle import get_bubble_info
from .runtime.base import ContainerRuntime


@dataclass
class CleanStatus:
    """Result of a cleanness check on a container."""

    clean: bool
    reasons: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if self.clean:
            return "clean"
        return ", ".join(format_reasons(self.reasons))


def check_clean(runtime: ContainerRuntime, name: str) -> CleanStatus:
    """Check if a container is clean (safe to pop without data loss).

    Requires the container to be running. Returns CleanStatus with
    error set if the container is not running or the check fails.
    """
    info = get_bubble_info(name)
    initial_commit = info.get("commit", "") if info else ""
    repo_short = ""
    if info and info.get("org_repo"):
        parts = info["org_repo"].split("/", 1)
        repo_short = parts[1] if len(parts) > 1 else parts[0]

    script = _build_check_script(initial_commit, repo_short)

    try:
        output = runtime.exec(name, ["su", "-", "user", "-c", script])
    except (RuntimeError, Exception) as e:
        msg = str(e).lower()
        if "not running" in msg or "not found" in msg:
            return CleanStatus(clean=False, error="not running")
        return CleanStatus(clean=False, error="check failed")

    return _parse_check_output(output)


def _build_check_script(initial_commit: str, repo_short: str) -> str:
    """Build the shell script that checks all cleanness conditions."""
    # Quote values to prevent shell injection from registry data
    q_repo = shlex.quote(repo_short)
    q_commit = shlex.quote(initial_commit)

    # The script outputs exactly one line: CLEAN=true/false REASONS=...
    return f"""\
CLEAN=true
REASONS=""
EXPECTED=$(echo {q_repo} | tr '[:upper:]' '[:lower:]')

# Check 1: no unexpected non-hidden items in home
ITEMS=$(ls /home/user/ 2>/dev/null || true)
if [ -n "$EXPECTED" ]; then
  if [ "$(echo "$ITEMS" | tr '[:upper:]' '[:lower:]')" != "$EXPECTED" ]; then
    CLEAN=false
    REASONS="${{REASONS}}extra_files;"
  fi
elif [ -n "$ITEMS" ]; then
  CLEAN=false
  REASONS="${{REASONS}}extra_files;"
fi

# Find the project directory
if [ -n "$EXPECTED" ] && [ -d "/home/user/$EXPECTED" ]; then
  PROJECT="/home/user/$EXPECTED"
else
  PROJECT=$(ls -d /home/user/*/ 2>/dev/null | head -1)
fi

# If there's a project dir, it must have a working git repo
if [ -n "$PROJECT" ]; then
  if [ ! -d "$PROJECT/.git" ]; then
    CLEAN=false
    REASONS="${{REASONS}}no_git;"
  elif ! command -v git >/dev/null 2>&1; then
    CLEAN=false
    REASONS="${{REASONS}}no_git;"
  else
    cd "$PROJECT"

    # Check 2: clean working tree
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
      CLEAN=false
      REASONS="${{REASONS}}dirty_worktree;"
    fi

    # Check 3: no stashes
    if [ -n "$(git stash list 2>/dev/null)" ]; then
      CLEAN=false
      REASONS="${{REASONS}}stashes;"
    fi

    # Check 4: no unpushed commits
    INITIAL={q_commit}
    while IFS= read -r branch; do
      [ -z "$branch" ] && continue
      UPSTREAM=$(git rev-parse --verify --quiet "$branch@{{upstream}}" 2>/dev/null || true)
      if [ -n "$UPSTREAM" ]; then
        AHEAD=$(git rev-list --count "$UPSTREAM".."$branch" 2>/dev/null || echo 0)
        if [ "$AHEAD" -gt 0 ]; then
          CLEAN=false
          REASONS="${{REASONS}}unpushed:$branch;"
        fi
      else
        if [ -n "$INITIAL" ]; then
          BRANCH_HEAD=$(git rev-parse "$branch" 2>/dev/null || true)
          if [ "$BRANCH_HEAD" != "$INITIAL" ]; then
            CLEAN=false
            REASONS="${{REASONS}}unpushed:$branch;"
          fi
        else
          CLEAN=false
          REASONS="${{REASONS}}untracked_branch:$branch;"
        fi
      fi
    done < <(git for-each-ref --format='%(refname:short)' refs/heads/)
  fi
fi

if [ -z "$REASONS" ]; then
  REASONS="none"
fi
echo "CLEAN=$CLEAN REASONS=$REASONS"
"""


def _parse_check_output(output: str) -> CleanStatus:
    """Parse the CLEAN=... REASONS=... output line."""
    for line in output.strip().splitlines():
        line = line.strip()
        if line.startswith("CLEAN="):
            parts = line.split(" ", 1)
            clean = parts[0] == "CLEAN=true"
            reasons_str = "none"
            if len(parts) > 1 and parts[1].startswith("REASONS="):
                reasons_str = parts[1][len("REASONS="):]
            if reasons_str == "none" or not reasons_str:
                reasons = []
            else:
                reasons = [r for r in reasons_str.rstrip(";").split(";") if r]
            return CleanStatus(clean=clean, reasons=reasons)
    return CleanStatus(clean=False, error="unexpected output")


def format_reasons(reasons: list[str]) -> list[str]:
    """Translate machine-readable reasons into human-readable strings."""
    result = []
    for r in reasons:
        if r == "extra_files":
            result.append("extra files in home")
        elif r == "dirty_worktree":
            result.append("uncommitted changes")
        elif r == "stashes":
            result.append("git stashes")
        elif r == "no_git":
            result.append("no git repository")
        elif r.startswith("unpushed:"):
            branch = r.split(":", 1)[1]
            result.append(f"unpushed commits on {branch}")
        elif r.startswith("untracked_branch:"):
            branch = r.split(":", 1)[1]
            result.append(f"untracked branch {branch}")
        else:
            result.append(r)
    return result
