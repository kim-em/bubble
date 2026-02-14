"""Configuration management for lean-bubbles."""

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w

# Override with LEAN_BUBBLES_HOME environment variable
DATA_DIR = Path(os.environ.get("LEAN_BUBBLES_HOME", Path.home() / ".lean-bubbles"))
CONFIG_FILE = DATA_DIR / "config.toml"
REGISTRY_FILE = DATA_DIR / "registry.json"
GIT_DIR = DATA_DIR / "git"
LAKE_CACHE_DIR = DATA_DIR / "lake-cache"

DEFAULT_CONFIG = {
    "runtime": {
        "backend": "incus",
        "colima_cpu": os.cpu_count() or 4,
        "colima_memory": 16,
        "colima_disk": 60,
        "colima_vm_type": "vz",
    },
    "git": {
        "shared_repos": [
            "leanprover-community/mathlib4",
            "leanprover/lean4",
            "leanprover-community/batteries",
        ],
        "update_interval": "1h",
    },
    "images": {
        "refresh": "weekly",
    },
    "network": {
        "allowlist": [
            "github.com",
            "*.githubusercontent.com",
            "objects.githubusercontent.com",
            "releases.lean-lang.org",
        ],
    },
}

# Well-known repos and their GitHub URLs
KNOWN_REPOS = {
    "mathlib4": "leanprover-community/mathlib4",
    "mathlib": "leanprover-community/mathlib4",
    "lean4": "leanprover/lean4",
    "lean": "leanprover/lean4",
    "batteries": "leanprover-community/batteries",
    "std4": "leanprover-community/batteries",
    "proofwidgets4": "leanprover-community/ProofWidgets4",
    "aesop": "leanprover-community/aesop",
    "quote4": "leanprover-community/quote4",
    "doc-gen4": "leanprover-community/doc-gen4",
}


def ensure_dirs():
    """Create data directories if they don't exist."""
    for d in [DATA_DIR, GIT_DIR, LAKE_CACHE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load config, creating default if it doesn't exist."""
    ensure_dirs()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            user_config = tomllib.load(f)
        # Merge with defaults (user overrides)
        config = _deep_merge(DEFAULT_CONFIG, user_config)
    else:
        config = DEFAULT_CONFIG.copy()
        save_config(config)
    return config


def save_config(config: dict):
    """Save config to disk."""
    ensure_dirs()
    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(config, f)


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts, with override taking precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_repo(name: str) -> str:
    """Resolve a short repo name to org/repo format."""
    if "/" in name:
        return name
    lower = name.lower()
    if lower in KNOWN_REPOS:
        return KNOWN_REPOS[lower]
    raise ValueError(
        f"Unknown repo '{name}'. Use org/repo format or one of: {', '.join(KNOWN_REPOS.keys())}"
    )


def repo_short_name(full_name: str) -> str:
    """Get the short name from org/repo format."""
    return full_name.split("/")[-1].lower()
