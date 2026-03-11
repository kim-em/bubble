"""Configuration management for bubble."""

import os
import sys
from dataclasses import dataclass, field
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


@dataclass
class MountSpec:
    """A user-specified host directory mount."""

    source: str  # host path (~ expanded)
    target: str  # container path
    readonly: bool = True  # default read-only
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_cli(cls, spec: str) -> "MountSpec":
        """Parse a --mount flag value: /host/path:/container/path[:ro|rw]"""
        parts = spec.split(":")
        if len(parts) < 2:
            raise ValueError(
                f"Invalid mount spec {spec!r}: expected /host/path:/container/path[:ro|rw]"
            )
        # Check for mode suffix
        mode = "ro"
        if len(parts) >= 3 and parts[-1] in ("ro", "rw"):
            mode = parts.pop()
        elif len(parts) >= 3 and not parts[-1].startswith("/"):
            # Third field present but not a valid mode — reject it
            raise ValueError(f"Invalid mount mode {parts[-1]!r} in {spec!r}: expected 'ro' or 'rw'")
        # Rejoin remaining parts — source:target with possible extra colons
        if len(parts) < 2:
            raise ValueError(
                f"Invalid mount spec {spec!r}: expected /host/path:/container/path[:ro|rw]"
            )
        # Target starts with / so split on ":/"
        raw = ":".join(parts)
        idx = raw.find(":/")
        if idx == -1:
            raise ValueError(f"Invalid mount spec {spec!r}: container path must be absolute")
        source = raw[:idx]
        target = raw[idx + 1 :]
        source = str(Path(source).expanduser())
        return cls(source=source, target=target, readonly=(mode == "ro"))

    @classmethod
    def from_config(cls, entry: dict) -> "MountSpec":
        """Parse a [[mounts]] config entry."""
        source = entry.get("source", "")
        target = entry.get("target", "")
        if not source or not target:
            raise ValueError(f"Mount entry missing 'source' or 'target': {entry}")
        mode = entry.get("mode", "ro")
        if mode not in ("ro", "rw"):
            raise ValueError(f"Invalid mount mode {mode!r}: expected 'ro' or 'rw'")
        exclude = entry.get("exclude", [])
        if isinstance(exclude, str):
            exclude = [exclude]
        for e in exclude:
            _validate_exclude(e)
        source = str(Path(source).expanduser())
        return cls(source=source, target=target, readonly=(mode == "ro"), exclude=exclude)


def _validate_exclude(entry: str) -> None:
    """Validate an exclude entry is a simple relative subdirectory name."""
    if not entry:
        raise ValueError("Empty exclude entry")
    if entry.startswith("/"):
        raise ValueError(f"Exclude entry must be relative, not absolute: {entry!r}")
    if ".." in entry.split("/"):
        raise ValueError(f"Exclude entry must not contain '..': {entry!r}")


CLAUDE_CONFIG_DIR = Path.home() / ".claude"

# Specific items from ~/.claude to mount read-only into containers.
# Only these are mounted; session history and transient state are
# excluded by omission.
_CLAUDE_CONFIG_ITEMS = [
    "CLAUDE.md",
    "settings.json",
    "skills",
    "keybindings.json",
    ".credentials.json",
    ".current-account",
]


def claude_config_mounts() -> list[MountSpec]:
    """Return read-only mounts for Claude Code config files that exist on the host.

    Mounts specific files/directories from ~/.claude into /home/user/.claude/
    inside containers, giving Claude Code sessions access to global config.
    """
    mounts = []
    if not CLAUDE_CONFIG_DIR.is_dir():
        return mounts
    for item in _CLAUDE_CONFIG_ITEMS:
        source = CLAUDE_CONFIG_DIR / item
        if source.exists():
            target = f"/home/user/.claude/{item}"
            mounts.append(MountSpec(source=str(source), target=target, readonly=True))
    return mounts


def parse_mounts(config: dict, cli_mounts: tuple[str, ...] = ()) -> list[MountSpec]:
    """Merge mounts from config file and CLI flags.

    CLI mounts take precedence (appended after config mounts).
    """
    mounts = []
    for entry in config.get("mounts", []):
        mounts.append(MountSpec.from_config(entry))
    for spec in cli_mounts:
        mounts.append(MountSpec.from_cli(spec))
    # Check for duplicate container targets
    seen: set[str] = set()
    for m in mounts:
        if m.target in seen:
            raise ValueError(f"Duplicate mount target: {m.target}")
        seen.add(m.target)
    return mounts
