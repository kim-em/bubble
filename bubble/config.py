"""Configuration management for bubble."""

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w

# Override with BUBBLE_HOME environment variable
DATA_DIR = Path(os.environ.get("BUBBLE_HOME", Path.home() / ".bubble"))
CONFIG_FILE = DATA_DIR / "config.toml"
REGISTRY_FILE = DATA_DIR / "registry.json"
GIT_DIR = DATA_DIR / "git"
REPOS_FILE = DATA_DIR / "repos.json"
CLOUD_STATE_FILE = DATA_DIR / "cloud.json"
CLOUD_KEY_FILE = DATA_DIR / "cloud_key"
CLOUD_KNOWN_HOSTS = DATA_DIR / "known_hosts"

DEFAULT_CONFIG = {
    "editor": "vscode",
    "runtime": {
        "backend": "incus",
        "colima_cpu": os.cpu_count() or 4,
        "colima_memory": 16,
        "colima_disk": 60,
        "colima_vm_type": "vz",
    },
    "images": {
        "refresh": "weekly",
    },
    "network": {
        "allowlist": [
            "github.com",
            "raw.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
            "codeload.githubusercontent.com",
        ],
    },
    "relay": {
        "enabled": False,
        "port": 7653,
    },
    "remote": {
        "default_host": "",
    },
    "cloud": {
        "provider": "hetzner",
        "server_type": "",
        "location": "fsn1",
        "server_name": "bubble-cloud",
        "default": False,
    },
}


def ensure_dirs():
    """Create data directories if they don't exist."""
    for d in [DATA_DIR, GIT_DIR]:
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


def repo_short_name(full_name: str) -> str:
    """Get the short name from org/repo format."""
    return full_name.split("/")[-1].lower()
