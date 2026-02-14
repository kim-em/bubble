"""Container name generation."""

import re
from datetime import date


def generate_name(repo_short: str, source: str, identifier: str) -> str:
    """Generate a container name from repo, source type, and identifier.

    Examples:
        generate_name("mathlib4", "pr", "12345") → "mathlib4-pr-12345"
        generate_name("batteries", "branch", "fix-grind") → "batteries-branch-fix-grind"
        generate_name("lean4", "main", "") → "lean4-main-20260213"
    """
    if source == "main" and not identifier:
        identifier = date.today().strftime("%Y%m%d")

    # Sanitize: lowercase, replace non-alphanumeric with hyphens, collapse multiples
    parts = [repo_short, source, identifier]
    name = "-".join(p for p in parts if p)
    name = re.sub(r"[^a-z0-9-]", "-", name.lower())
    name = re.sub(r"-+", "-", name).strip("-")

    # Incus names must start with a letter
    if name and not name[0].isalpha():
        name = "b-" + name

    return name


def deduplicate_name(name: str, existing_names: set[str]) -> str:
    """Add a numeric suffix if the name already exists."""
    if name not in existing_names:
        return name
    for i in range(2, 1000):
        candidate = f"{name}-{i}"
        if candidate not in existing_names:
            return candidate
    raise RuntimeError(f"Could not find unique name for '{name}'")
