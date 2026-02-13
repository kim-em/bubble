"""PR description metadata injection and parsing.

Stores lean-bubbles session state as an invisible HTML comment in PR descriptions.
This allows session reconstitution from a PR URL on any machine.

Format:
<!-- lean-bubbles: {"session_id":"...","base_image":"...","branch":"...","commit":"...",...} -->
"""

import json
import re
import subprocess

METADATA_PREFIX = "<!-- lean-bubbles: "
METADATA_SUFFIX = " -->"
METADATA_PATTERN = re.compile(
    r"<!-- lean-bubbles: ({.*?}) -->",
    re.DOTALL,
)


def inject_metadata(pr_url: str, metadata: dict):
    """Inject lean-bubbles metadata into a PR description.

    Args:
        pr_url: GitHub PR URL (e.g., https://github.com/org/repo/pull/123)
        metadata: Dict of session state to store
    """
    # Get current PR body
    current_body = _get_pr_body(pr_url)

    # Remove any existing lean-bubbles metadata
    cleaned = METADATA_PATTERN.sub("", current_body).rstrip()

    # Append new metadata
    meta_json = json.dumps(metadata, separators=(",", ":"))
    new_body = f"{cleaned}\n\n{METADATA_PREFIX}{meta_json}{METADATA_SUFFIX}\n"

    # Update PR
    _set_pr_body(pr_url, new_body)


def extract_metadata(pr_url: str) -> dict | None:
    """Extract lean-bubbles metadata from a PR description.

    Returns None if no metadata found.
    """
    body = _get_pr_body(pr_url)
    match = METADATA_PATTERN.search(body)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse a GitHub PR URL into (org/repo, pr_number).

    Handles:
      https://github.com/org/repo/pull/123
      org/repo#123
    """
    # Full URL format
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))

    # Short format: org/repo#123
    m = re.match(r"([^/]+/[^/]+)#(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))

    raise ValueError(f"Cannot parse PR URL: {url}")


def _get_pr_body(pr_url: str) -> str:
    """Get the body of a PR using gh CLI."""
    result = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _set_pr_body(pr_url: str, body: str):
    """Set the body of a PR using gh CLI."""
    subprocess.run(
        ["gh", "pr", "edit", pr_url, "--body", body],
        capture_output=True, text=True, check=True,
    )
