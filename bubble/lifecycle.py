"""Container lifecycle management: registry tracking."""

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import REGISTRY_FILE


@contextmanager
def _registry_lock():
    """Acquire an exclusive file lock for the registry to prevent concurrent modifications."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_FILE.with_suffix(".lock")
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _read_registry() -> dict:
    """Read the registry without locking or migration. Internal use only."""
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return {"bubbles": {}}


def load_registry() -> dict:
    """Load the bubble registry, migrating away any legacy native entries."""
    registry = _read_registry()
    bubbles = registry.get("bubbles", {})
    if any(info.get("native") for info in bubbles.values()):
        with _registry_lock():
            registry = _read_registry()
            bubbles = registry.get("bubbles", {})
            registry["bubbles"] = {
                name: info for name, info in bubbles.items() if not info.get("native")
            }
            _save_registry(registry)
    return registry


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
    base_image: str = "",
    remote_host: str = "",
    project_dir: str = "",
):
    """Record a bubble's creation in the registry."""
    with _registry_lock():
        registry = _read_registry()
        entry = {
            "org_repo": org_repo,
            "branch": branch,
            "commit": commit,
            "pr": pr,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if base_image:
            entry["base_image"] = base_image
        if remote_host:
            entry["remote_host"] = remote_host
        if project_dir:
            entry["project_dir"] = project_dir
        registry["bubbles"][name] = entry
        _save_registry(registry)


def get_bubble_info(name: str) -> dict | None:
    """Get registry info for a bubble."""
    registry = load_registry()
    return registry["bubbles"].get(name)


def unregister_bubble(name: str):
    """Remove a bubble from the registry."""
    with _registry_lock():
        registry = _read_registry()
        registry["bubbles"].pop(name, None)
        _save_registry(registry)


def prune_stale_entries(live_containers: set[str]) -> list[str]:
    """Remove registry entries for local containers that no longer exist.

    Only prunes entries that are local (not remote).
    Returns the list of pruned names.
    """
    with _registry_lock():
        registry = _read_registry()
        stale = []
        for name, info in registry.get("bubbles", {}).items():
            if info.get("remote_host"):
                continue
            if name not in live_containers:
                stale.append(name)
        if stale:
            for name in stale:
                registry["bubbles"].pop(name, None)
            _save_registry(registry)

    # Best-effort SSH config cleanup (outside the lock, non-fatal)
    if stale:
        try:
            from .vscode import remove_ssh_config

            for name in stale:
                remove_ssh_config(name)
        except OSError:
            pass
    return stale
