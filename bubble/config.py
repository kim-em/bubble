"""Configuration management for bubble."""

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    "ai": {
        "preferred": "claude",
        "second_opinion": "auto",
        "second_opinion_provider": "codex",
        "autonomy": "plan",
    },
    "claude": {
        "credentials": True,
    },
    "codex": {
        "credentials": True,
    },
    "security": {},
    "tools": {},
}


def ensure_dirs():
    """Create data directories if they don't exist."""
    for d in [DATA_DIR, GIT_DIR, NATIVE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def is_first_run() -> bool:
    """Check whether this is the first run (config file doesn't exist yet)."""
    return not CONFIG_FILE.exists()


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
    """Deep merge two dicts, with override taking precedence.

    All inherited values from base are deep-copied to prevent mutation
    of DEFAULT_CONFIG or other shared structures.
    """
    result = {}
    for key, value in base.items():
        if key in override and isinstance(value, dict) and isinstance(override[key], dict):
            result[key] = _deep_merge(value, override[key])
        elif key in override:
            result[key] = override[key]
        else:
            # Deep-copy all inherited values (dicts, lists, etc.) to prevent aliasing
            result[key] = copy.deepcopy(value)
    for key, value in override.items():
        if key not in base:
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


class SafeConfigDir:
    """Manages safe mounting of a tool's config directory into containers.

    Handles symlink-escape checking, mount generation, and credential detection
    for a tool config directory (e.g., ~/.claude, ~/.codex).
    """

    def __init__(
        self,
        base_dir: Path,
        container_dir: str,
        config_items: list[str],
        credential_items: list[str],
    ):
        self.base_dir = base_dir
        self.container_dir = container_dir
        self.config_items = config_items
        self.credential_items = credential_items

    def safe_path(self, item: str) -> Path | None:
        """Return resolved path for a config item, or None if unsafe.

        Rejects symlinks that escape the base directory to prevent exposing
        arbitrary host files into containers.
        """
        source = self.base_dir / item
        if not source.exists():
            return None
        resolved = source.resolve()
        base_resolved = self.base_dir.resolve()
        try:
            resolved.relative_to(base_resolved)
        except ValueError:
            return None
        return resolved

    def config_mounts(self, include_credentials: bool = True) -> list[MountSpec]:
        """Return read-only mounts for config files that exist on the host.

        Args:
            include_credentials: If True, also mount credential files.
        """
        mounts = []
        if not self.base_dir.is_dir():
            return mounts
        items = list(self.config_items)
        if include_credentials:
            items.extend(self.credential_items)
        for item in items:
            resolved = self.safe_path(item)
            if resolved is not None:
                target = f"{self.container_dir}/{item}"
                mounts.append(MountSpec(source=str(resolved), target=target, readonly=True))
        return mounts

    def has_credentials(self) -> bool:
        """Check if the host has credential files for this tool."""
        if not self.base_dir.is_dir():
            return False
        return any((self.base_dir / item).exists() for item in self.credential_items)


CLAUDE_CONFIG_DIR = Path.home() / ".claude"

CLAUDE_CONFIG = SafeConfigDir(
    base_dir=CLAUDE_CONFIG_DIR,
    container_dir="/home/user/.claude",
    config_items=["CLAUDE.md", "settings.json", "skills", "keybindings.json", "commands"],
    credential_items=[".credentials.json"],
)


def claude_config_mounts(include_credentials: bool = True) -> list[MountSpec]:
    """Return read-only mounts for Claude Code config files that exist on the host."""
    return CLAUDE_CONFIG.config_mounts(include_credentials)


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


def editor_config_mounts(editor: str, config: dict | None = None) -> list[MountSpec]:
    """Return mounts for editor config directories that exist on the host.

    Config directories are mounted read-only (with exclusions for known
    writable subdirectories). Data/state/cache directories are mounted
    read-write by default so plugin managers and caches can function,
    but can be made read-only via security.editor_data_write = off.

    Only returns mounts for directories that actually exist on the host.
    Data dirs are only mounted if a config dir was found.
    """
    from .security import is_enabled

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
    # Mount data dirs (read-write unless editor_data_write is off)
    data_writable = is_enabled(config or {}, "editor_data_write")
    for host_path, container_path in spec["data"]:
        resolved = _safe_editor_path(host_path)
        if resolved is not None:
            mounts.append(
                MountSpec(source=str(resolved), target=container_path, readonly=not data_writable)
            )
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


CODEX_CONFIG_DIR = Path.home() / ".codex"

CODEX_CONFIG = SafeConfigDir(
    base_dir=CODEX_CONFIG_DIR,
    container_dir="/home/user/.codex",
    config_items=["config.toml"],
    credential_items=["auth.json"],
)


def codex_config_mounts(include_credentials: bool = True) -> list[MountSpec]:
    """Return read-only mounts for Codex config files that exist on the host."""
    return CODEX_CONFIG.config_mounts(include_credentials)


AI_PROJECTS_DIR = DATA_DIR / "ai-projects"


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


def maybe_symlink_ai_projects(config: dict | None = None, notices=None) -> None:
    """Print an informational message if ai-projects could be symlinked.

    If ~/.claude/projects/ is inside a git repo and ~/.bubble/ai-projects/ is a real
    directory (not already a symlink), print a one-time hint about the
    ``bubble config symlink-ai-projects`` command. Never prompts interactively.

    Suppressed when ``ai_projects_symlink = "no"`` is set in config.
    """
    # Respect opt-out config
    if config:
        if config.get("ai_projects_symlink") == "no":
            return

    claude_projects = CLAUDE_CONFIG_DIR / "projects"
    bubble_projects = AI_PROJECTS_DIR

    # Nothing to do if ~/.claude/projects/ doesn't exist or isn't in a git repo
    if not claude_projects.is_dir() or not _is_inside_git_repo(claude_projects):
        return

    # Already a symlink — nothing to do
    if bubble_projects.is_symlink():
        return

    # If ~/.bubble/ai-projects/ doesn't exist yet, just create the symlink
    if not bubble_projects.exists():
        bubble_projects.parent.mkdir(parents=True, exist_ok=True)
        bubble_projects.symlink_to(claude_projects)
        return

    # It's a real directory — print informational message (no prompt)
    import click

    if notices:
        notices.begin()
    click.echo(
        "~/.claude/projects is git-tracked. AI sessions within bubbles are stored\n"
        "in ~/.bubble/ai-projects. To link that directory into the git-tracked\n"
        "location (existing data is merged, not overwritten), run:\n"
        "\n"
        "  bubble config symlink-ai-projects\n"
        "\n"
        'To suppress this message, set ai_projects_symlink = "no" in '
        "~/.bubble/config.toml.",
        err=True,
    )


def do_symlink_ai_projects() -> bool:
    """Link ~/.bubble/ai-projects/ to ~/.claude/projects/ via symlink.

    Merges existing contents from bubble-projects into claude-projects before
    creating the symlink. Aborts if any files conflict (exist in both
    locations).

    Anything left in ~/.bubble/ai-projects/ after the merge (residual empty
    directories, or files that appeared during the merge from a concurrently-
    running bubble) is renamed aside to ai-projects.old.<timestamp>.<rand>/
    rather than deleted. The user can inspect and remove that directory once
    they're satisfied the merge worked.

    Returns True if the symlink was created, False otherwise.
    """
    import click

    claude_projects = CLAUDE_CONFIG_DIR / "projects"
    bubble_projects = AI_PROJECTS_DIR

    if not claude_projects.is_dir():
        click.echo(f"{claude_projects} does not exist.", err=True)
        return False

    if not _is_inside_git_repo(claude_projects):
        click.echo(f"{claude_projects} is not inside a git repository.", err=True)
        return False

    if bubble_projects.is_symlink():
        target = bubble_projects.resolve()
        if target == claude_projects.resolve():
            click.echo(f"{bubble_projects} is already a symlink to {claude_projects}.")
            return True
        click.echo(f"{bubble_projects} is a symlink to {target}, not {claude_projects}.", err=True)
        return False

    if not bubble_projects.exists():
        bubble_projects.parent.mkdir(parents=True, exist_ok=True)
        bubble_projects.symlink_to(claude_projects)
        click.echo(f"Created symlink: {bubble_projects} -> {claude_projects}")
        return True

    if not bubble_projects.is_dir():
        click.echo(f"{bubble_projects} exists but is not a directory.", err=True)
        return False

    # Pass 1: check for conflicts before moving anything
    conflicts = _find_conflicts(bubble_projects, claude_projects)
    if conflicts:
        for c in conflicts:
            click.echo(f"  Conflict: {c.relative_to(bubble_projects)}")
        click.echo(
            f"\nAborted: {len(conflicts)} file(s) in {bubble_projects} conflict "
            f"with existing files in {claude_projects}.\n"
            "Resolve these conflicts manually, then re-run this command.",
            err=True,
        )
        return False

    # Pass 2: no conflicts, safe to merge
    _merge_dir(bubble_projects, claude_projects)

    # Rename the (now-residual) source aside instead of rmtree'ing it.
    # mkdtemp gives a guaranteed-unique suffix; rename() needs the target
    # to be absent so we remove the empty placeholder it creates.
    backup = Path(
        tempfile.mkdtemp(
            prefix=f"ai-projects.old.{time.strftime('%Y%m%d-%H%M%S')}.",
            dir=str(bubble_projects.parent),
        )
    )
    backup.rmdir()
    bubble_projects.rename(backup)
    try:
        bubble_projects.symlink_to(claude_projects)
    except OSError:
        # Symlink failed: roll the directory back so the user is not left
        # without ~/.bubble/ai-projects/ at all.
        backup.rename(bubble_projects)
        raise

    click.echo(f"Created symlink: {bubble_projects} -> {claude_projects}")
    click.echo(
        f"Residual data from the merge is at {backup}. "
        "Inspect and remove once you're satisfied the merge worked."
    )
    return True


def _find_conflicts(src: Path, dest: Path) -> list[Path]:
    """Recursively find files in src that conflict with existing files in dest."""
    conflicts: list[Path] = []
    for item in src.iterdir():
        target = dest / item.name
        if not target.exists():
            pass
        elif item.is_dir() and target.is_dir():
            conflicts.extend(_find_conflicts(item, target))
        else:
            conflicts.append(item)
    return conflicts


def _merge_dir(src: Path, dest: Path) -> None:
    """Recursively move items from src into dest. Caller must ensure no conflicts."""
    for item in src.iterdir():
        target = dest / item.name
        if not target.exists():
            shutil.move(str(item), str(target))
        elif item.is_dir() and target.is_dir():
            _merge_dir(item, target)
