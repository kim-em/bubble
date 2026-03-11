"""Claude Code skill management for bubble."""

import difflib
from importlib import resources
from pathlib import Path

SKILL_NAME = "bubble"
SKILL_FILENAME = "SKILL.md"
CLAUDE_DIR = Path.home() / ".claude"
SKILLS_DIR = CLAUDE_DIR / "skills" / SKILL_NAME
INSTALLED_SKILL = SKILLS_DIR / SKILL_FILENAME


def _bundled_skill_content() -> str:
    """Read the skill file bundled with the package."""
    ref = resources.files("bubble").joinpath("data/skill.md")
    return ref.read_text(encoding="utf-8")


def claude_code_detected() -> bool:
    """Check if Claude Code is set up (i.e. ~/.claude/ exists)."""
    return CLAUDE_DIR.is_dir()


def is_installed() -> bool:
    """Check if the bubble skill is installed."""
    return INSTALLED_SKILL.is_file()


def _read_installed() -> str | None:
    """Read the installed skill file, returning None if unreadable."""
    if not is_installed():
        return None
    try:
        return INSTALLED_SKILL.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_up_to_date() -> bool:
    """Check if the installed skill matches the bundled version."""
    installed = _read_installed()
    if installed is None:
        return False
    return installed == _bundled_skill_content()


def diff_skill() -> str:
    """Return a unified diff between installed and bundled skill, or empty string."""
    installed = _read_installed()
    if installed is None:
        return ""
    installed_lines = installed.splitlines(keepends=True)
    bundled_lines = _bundled_skill_content().splitlines(keepends=True)
    return "".join(difflib.unified_diff(installed_lines, bundled_lines, "installed", "bundled"))


def install_skill() -> str:
    """Install or update the skill file. Returns a status message."""
    if not claude_code_detected():
        return "~/.claude/ not found — Claude Code not detected. Skipping skill install."

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    content = _bundled_skill_content()
    INSTALLED_SKILL.write_text(content, encoding="utf-8")
    return f"Installed bubble skill to {INSTALLED_SKILL}"


def uninstall_skill() -> str:
    """Remove the installed skill. Returns a status message."""
    if not is_installed():
        return "Bubble skill is not installed."
    INSTALLED_SKILL.unlink()
    # Remove the directory if empty
    try:
        SKILLS_DIR.rmdir()
    except OSError:
        pass
    return f"Removed bubble skill from {INSTALLED_SKILL}"
