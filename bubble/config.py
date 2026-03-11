"""Configuration management for bubble."""

import copy
import os
import shutil
import subprocess
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
NATIVE_DIR = DATA_DIR / "native"

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
    "claude": {
        "credentials": False,
    },
    "tools": {},
}


def ensure_dirs():
    """Create data directories if they don't exist."""
    for d in [DATA_DIR, GIT_DIR, NATIVE_DIR]:
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
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
    return config


def load_raw_config() -> dict:
    """Load the raw user config without merging defaults.

    Returns only what the user has explicitly set in config.toml.
    """
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    return {}


def save_config(config: dict):
    """Save config to disk."""
    ensure_dirs()
    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(config, f)


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts, with override taking precedence."""
    result = copy.deepcopy(base)
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
    "commands",
]

# Credential files — opt-in only (--claude-credentials).
_CLAUDE_CREDENTIAL_ITEMS = [
    ".credentials.json",
    ".current-account",
]


def _safe_claude_path(item: str) -> Path | None:
    """Return resolved path for a claude config item, or None if unsafe.

    Rejects symlinks that escape ~/.claude to prevent exposing arbitrary
    host files into containers.
    """
    source = CLAUDE_CONFIG_DIR / item
    if not source.exists():
        return None
    resolved = source.resolve()
    claude_resolved = CLAUDE_CONFIG_DIR.resolve()
    # Ensure the resolved path is inside ~/.claude
    try:
        resolved.relative_to(claude_resolved)
    except ValueError:
        return None
    return resolved


def claude_config_mounts(include_credentials: bool = False) -> list[MountSpec]:
    """Return read-only mounts for Claude Code config files that exist on the host.

    Mounts specific files/directories from ~/.claude into /home/user/.claude/
    inside containers, giving Claude Code sessions access to global config.

    Args:
        include_credentials: If True, also mount .credentials.json and
            .current-account. Off by default for security.
    """
    mounts = []
    if not CLAUDE_CONFIG_DIR.is_dir():
        return mounts
    items = list(_CLAUDE_CONFIG_ITEMS)
    if include_credentials:
        items.extend(_CLAUDE_CREDENTIAL_ITEMS)
    for item in items:
        resolved = _safe_claude_path(item)
        if resolved is not None:
            target = f"/home/user/.claude/{item}"
            mounts.append(MountSpec(source=str(resolved), target=target, readonly=True))
    return mounts


def has_claude_credentials() -> bool:
    """Check if the host has Claude credential files."""
    if not CLAUDE_CONFIG_DIR.is_dir():
        return False
    return any((CLAUDE_CONFIG_DIR / item).exists() for item in _CLAUDE_CREDENTIAL_ITEMS)


# Editor config directories to mount into containers.
# Config is read-only; data/state directories are mounted read-write so
# plugin managers and caches can function.
#
# For Emacs legacy layout (~/.emacs.d/), the entire directory is the config
# AND the data store. We mount it read-only with exclusions for known
# writable subdirectories, which get tmpfs overlays so Emacs can write to
# them without modifying the host.
_EDITOR_CONFIG = {
    "emacs": {
        # Config: XDG (~/.config/emacs/) preferred, fall back to ~/.emacs.d/
        "config": [
            (Path.home() / ".config" / "emacs", "/home/user/.config/emacs"),
            (Path.home() / ".emacs.d", "/home/user/.emacs.d"),
        ],
        # Data dirs: writable so plugin managers (straight.el, elpaca, etc.)
        # and byte-compilation can work.
        "data": [
            (Path.home() / ".local" / "share" / "emacs", "/home/user/.local/share/emacs"),
            (Path.home() / ".cache" / "emacs", "/home/user/.cache/emacs"),
        ],
        # Writable subdirectories within the config dir (for legacy ~/.emacs.d/ layout).
        # These get tmpfs overlays so the editor can write to them.
        "config_writable_subdirs": [
            "elpa",
            "eln-cache",
            "straight",
            "elpaca",
            "auto-save-list",
            "transient",
            ".cache",
        ],
    },
    "neovim": {
        "config": [
            (Path.home() / ".config" / "nvim", "/home/user/.config/nvim"),
        ],
        "data": [
            (Path.home() / ".local" / "share" / "nvim", "/home/user/.local/share/nvim"),
            (Path.home() / ".local" / "state" / "nvim", "/home/user/.local/state/nvim"),
            (Path.home() / ".cache" / "nvim", "/home/user/.cache/nvim"),
        ],
        "config_writable_subdirs": [],
    },
}

# Directories that are considered safe parents for editor config paths.
# Symlinks that resolve outside these trees are rejected.
_EDITOR_SAFE_ROOTS = [
    Path.home() / ".config",
    Path.home() / ".emacs.d",
    Path.home() / ".local",
    Path.home() / ".cache",
]


def _safe_editor_path(host_path: Path) -> Path | None:
    """Return host_path if it's a real directory within expected locations.

    Rejects symlinks that escape the expected config/data tree to prevent
    exposing arbitrary host directories into containers.
    """
    if not host_path.is_dir():
        return None
    resolved = host_path.resolve()
    for root in _EDITOR_SAFE_ROOTS:
        try:
            root_resolved = root.resolve()
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    return None


def editor_config_mounts(editor: str) -> list[MountSpec]:
    """Return mounts for editor config directories that exist on the host.

    Config directories are mounted read-only (with exclusions for known
    writable subdirectories). Data/state/cache directories are mounted
    read-write so plugin managers and caches can function.

    Only returns mounts for directories that actually exist on the host.
    Data dirs are only mounted if a config dir was found.
    """
    spec = _EDITOR_CONFIG.get(editor)
    if not spec:
        return []
    mounts: list[MountSpec] = []
    config_found = False
    writable_subdirs = spec.get("config_writable_subdirs", [])
    # Mount config dirs read-only (pick first that exists)
    for host_path, container_path in spec["config"]:
        resolved = _safe_editor_path(host_path)
        if resolved is not None:
            # Filter exclusions to only those that exist on the host
            exclude = [d for d in writable_subdirs if (resolved / d).exists()]
            mounts.append(
                MountSpec(
                    source=str(resolved), target=container_path, readonly=True, exclude=exclude
                )
            )
            config_found = True
            break  # Only mount the first matching config location
    if not config_found:
        return []
    # Mount data dirs read-write (all that exist, only if config was found)
    for host_path, container_path in spec["data"]:
        resolved = _safe_editor_path(host_path)
        if resolved is not None:
            mounts.append(MountSpec(source=str(resolved), target=container_path, readonly=False))
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


CLAUDE_PROJECTS_DIR = DATA_DIR / "claude-projects"


def _is_inside_git_repo(path: Path) -> bool:
    """Check if a path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def maybe_symlink_claude_projects() -> None:
    """Offer to replace ~/.bubble/claude-projects/ with a symlink to ~/.claude/projects/.

    If ~/.claude/projects/ is inside a git repo and ~/.bubble/claude-projects/ is a real
    directory (not already a symlink), prompt the user to replace it with a symlink.
    This lets bubble session state live inside the git-tracked directory and get
    synced across machines automatically.
    """
    import sys

    claude_projects = CLAUDE_CONFIG_DIR / "projects"
    bubble_projects = CLAUDE_PROJECTS_DIR

    # Nothing to do if ~/.claude/projects/ doesn't exist or isn't in a git repo
    if not claude_projects.is_dir() or not _is_inside_git_repo(claude_projects):
        return

    # Already a symlink — nothing to do
    if bubble_projects.is_symlink():
        return

    # If ~/.bubble/claude-projects/ doesn't exist yet, just create the symlink
    if not bubble_projects.exists():
        bubble_projects.parent.mkdir(parents=True, exist_ok=True)
        bubble_projects.symlink_to(claude_projects)
        return

    # Don't prompt if stdin is not a TTY (scripted/CI usage)
    if not sys.stdin.isatty():
        return

    # It's a real directory — prompt the user
    import click

    if not click.confirm(
        f"{claude_projects} is git-tracked. Replace {bubble_projects}\n"
        f"with a symlink to {claude_projects} so bubble session state is tracked too?",
        default=False,
    ):
        return

    # Merge existing contents into ~/.claude/projects/
    # Move unique items; for conflicts, copy bubble-only files into the
    # destination so nothing is silently lost.
    for child in bubble_projects.iterdir():
        dest = claude_projects / child.name
        if not dest.exists():
            shutil.move(str(child), str(dest))
        elif child.is_dir() and dest.is_dir():
            # Recursively merge directory contents that only exist in bubble
            _merge_dir(child, dest)
        else:
            click.echo(f"  Skipping {child.name} (already exists in {claude_projects})")

    # Replace with symlink — use rmtree since conflicts may leave remnants
    shutil.rmtree(str(bubble_projects))
    bubble_projects.symlink_to(claude_projects)


def _merge_dir(src: Path, dest: Path) -> None:
    """Recursively move items from src into dest, skipping existing names."""
    import click

    for item in src.iterdir():
        target = dest / item.name
        if not target.exists():
            shutil.move(str(item), str(target))
        elif item.is_dir() and target.is_dir():
            _merge_dir(item, target)
        else:
            click.echo(f"  Skipping {item.name} (already exists in {dest})")
