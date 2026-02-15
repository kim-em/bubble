"""Container lifecycle management: registry tracking."""

import json
from datetime import datetime, timezone

from .config import REGISTRY_FILE


def load_registry() -> dict:
    """Load the bubble registry."""
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return {"bubbles": {}}


def _save_registry(registry: dict):
    """Save the bubble registry atomically."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2) + "\n")
    tmp.rename(REGISTRY_FILE)


def register_bubble(
    name: str,
    org_repo: str,
    branch: str = "",
    commit: str = "",
    pr: int = 0,
    base_image: str = "base",
    remote_host: str = "",
):
    """Record a bubble's creation in the registry."""
    registry = load_registry()
    entry = {
        "org_repo": org_repo,
        "branch": branch,
        "commit": commit,
        "pr": pr,
        "base_image": base_image,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if remote_host:
        entry["remote_host"] = remote_host
    registry["bubbles"][name] = entry
    _save_registry(registry)


def get_bubble_info(name: str) -> dict | None:
    """Get registry info for a bubble."""
    registry = load_registry()
    return registry["bubbles"].get(name)


def unregister_bubble(name: str):
    """Remove a bubble from the registry."""
    registry = load_registry()
    registry["bubbles"].pop(name, None)
    _save_registry(registry)
