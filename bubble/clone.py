"""Repo cloning, checkout, and branch creation inside containers."""

import shlex
import subprocess

from .git_store import github_url
from .output import step


def _get_pr_metadata(owner: str, repo: str, pr_number: str) -> tuple[str, str, str] | None:
    """Query GitHub API for PR head branch info.

    Returns (head_ref, head_repo, clone_url) or None.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/pulls/{pr_number}",
                "--jq",
                ".head.ref,.head.repo.full_name,.head.repo.clone_url",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if len(lines) == 3:
                return lines[0], lines[1], lines[2]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def fetch_pr_metadata(t):
    """Fetch ``(head_ref, head_repo, clone_url)`` for a PR target, or None.

    Thin public wrapper around :func:`_get_pr_metadata` so callers can
    fetch PR metadata once and reuse it (for fork detection before auth
    setup and again for checkout) rather than querying GitHub twice.
    """
    if t.kind != "pr":
        return None
    return _get_pr_metadata(t.owner, t.repo, t.ref)


def pr_fork_repo(t, pr_meta=None) -> str | None:
    """Return the PR head repo ``owner/repo`` when it's a fork, else None.

    For a PR target whose head lives in a fork (head repo != base repo),
    returns that fork's ``owner/repo``. Used to auto-authorize git
    fetch/push to the fork so the standard fork-PR workflow works inside a
    bubble. Returns None for non-PR targets, same-repo PRs, or when the PR
    metadata can't be fetched (e.g. gh unavailable).

    ``pr_meta`` may be a previously-fetched ``(head_ref, head_repo,
    clone_url)`` tuple to avoid re-querying GitHub.
    """
    if t.kind != "pr":
        return None
    if pr_meta is None:
        pr_meta = _get_pr_metadata(t.owner, t.repo, t.ref)
    if not pr_meta:
        return None
    _head_ref, head_repo, _clone_url = pr_meta
    if head_repo and head_repo.lower() != t.org_repo.lower():
        return head_repo
    return None


def clone_and_checkout(runtime, name, t, mount_name, short, pr_meta=None) -> str:
    """Clone the repo and checkout the appropriate ref. Returns the checkout branch name."""
    url = github_url(t.org_repo)
    q_short = shlex.quote(short)
    step(f"Cloning {t.org_repo} (using shared objects)...")
    runtime.exec(
        name,
        [
            "su",
            "-",
            "user",
            "-c",
            f"git clone --reference /shared/git/{mount_name} {url} /home/user/{q_short}",
        ],
    )

    checkout_branch = ""
    if t.kind == "issue":
        branch_name = f"issue-{t.ref}"
        step(f"Creating branch '{branch_name}' for issue #{t.ref}...")
        q_branch = shlex.quote(branch_name)
        runtime.exec(
            name,
            [
                "su",
                "-",
                "user",
                "-c",
                f"cd /home/user/{q_short} && git checkout -b {q_branch}",
            ],
        )
        checkout_branch = branch_name
    elif t.kind == "pr":
        step(f"Checking out PR #{t.ref}...")
        if pr_meta is None:
            pr_meta = _get_pr_metadata(t.owner, t.repo, t.ref)
        pr_checkout_ok = False
        if pr_meta:
            head_ref, head_repo, clone_url = pr_meta
            is_fork = head_repo.lower() != t.org_repo.lower()
            q_head = shlex.quote(head_ref)

            try:
                if is_fork:
                    # Fork PR: add fork remote, fetch branch, checkout with tracking
                    fork_owner = head_repo.split("/")[0]
                    q_owner = shlex.quote(fork_owner)
                    q_url = shlex.quote(clone_url)
                    runtime.exec(
                        name,
                        [
                            "su",
                            "-",
                            "user",
                            "-c",
                            f"cd /home/user/{q_short}"
                            f" && (git remote add {q_owner} {q_url} 2>/dev/null"
                            f" || git remote set-url {q_owner} {q_url})"
                            f" && git fetch {q_owner}"
                            f" +refs/heads/{q_head}:refs/remotes/{q_owner}/{q_head}"
                            f" && git checkout -b {q_head} --track {q_owner}/{q_head}",
                        ],
                    )
                else:
                    # Same-repo PR: fetch branch, checkout with tracking
                    runtime.exec(
                        name,
                        [
                            "su",
                            "-",
                            "user",
                            "-c",
                            f"cd /home/user/{q_short}"
                            f" && git fetch origin"
                            f" +refs/heads/{q_head}:refs/remotes/origin/{q_head}"
                            f" && git checkout -b {q_head} --track origin/{q_head}",
                        ],
                    )
                checkout_branch = head_ref
                pr_checkout_ok = True
            except RuntimeError:
                # Branch may have been deleted; fall through to pull ref fallback
                pass

        if not pr_checkout_ok:
            # Fallback: gh unavailable or API error
            checkout_branch = f"pr-{t.ref}"
            q_branch = shlex.quote(checkout_branch)
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "user",
                    "-c",
                    f"cd /home/user/{q_short} && git fetch origin"
                    f" pull/{t.ref}/head:{q_branch} && git checkout {q_branch}",
                ],
            )
    elif t.kind == "branch" and t.new_branch:
        base = t.base_ref if t.base_ref else "HEAD"
        q_base = shlex.quote(base)
        q_branch = shlex.quote(t.ref)
        if t.base_ref:
            step(f"Creating branch '{t.ref}' off '{t.base_ref}'...")
            # Fetch the base ref first if it's not HEAD
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "user",
                    "-c",
                    f"cd /home/user/{q_short} && git fetch origin {q_base}"
                    f" && git checkout -b {q_branch} FETCH_HEAD",
                ],
            )
        else:
            step(f"Creating branch '{t.ref}' off default branch...")
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "user",
                    "-c",
                    f"cd /home/user/{q_short} && git checkout -b {q_branch}",
                ],
            )
        checkout_branch = t.ref
    elif t.kind == "branch":
        step(f"Checking out branch '{t.ref}'...")
        checkout_branch = t.ref
        q_branch = shlex.quote(t.ref)
        try:
            runtime.exec(
                name,
                ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git switch {q_branch}"],
            )
        except RuntimeError:
            if t.local_path:
                runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"cd /home/user/{q_short} && git fetch /shared/git/{mount_name}"
                        f" {q_branch}:{q_branch} && git switch {q_branch}",
                    ],
                )
            else:
                raise
    elif t.kind == "commit":
        step(f"Checking out commit {t.ref[:12]}...")
        q_commit = shlex.quote(t.ref)
        runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git checkout {q_commit}"],
        )

    return checkout_branch
