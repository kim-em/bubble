"""Lean 4 language hook."""

import re
import subprocess
from pathlib import Path

from . import Hook

# Matches stable releases (v4.16.0) and release candidates (v4.16.0-rc2)
_STABLE_OR_RC_RE = re.compile(r"^v\d+\.\d+\.\d+(-rc\d+)?$")


def _read_lean_toolchain(bare_repo_path: Path, ref: str) -> str | None:
    """Read the lean-toolchain file content from a bare repo at a given ref."""
    try:
        result = subprocess.run(
            ["git", "-C", str(bare_repo_path), "show", f"{ref}:lean-toolchain"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _parse_lean_version(toolchain_str: str) -> str | None:
    """Extract the version tag from a lean-toolchain string.

    Handles formats like:
        leanprover/lean4:v4.16.0
        leanprover/lean4:v4.16.0-rc2
        leanprover/lean4:nightly-2024-01-01  (returns None)

    Returns the version (e.g. 'v4.16.0') if it's a stable or RC release, else None.
    """
    # Strip the repository prefix if present
    if ":" in toolchain_str:
        version = toolchain_str.split(":", 1)[1]
    else:
        version = toolchain_str

    if _STABLE_OR_RC_RE.match(version):
        return version
    return None


class LeanHook(Hook):
    """Hook for Lean 4 projects (detected by lean-toolchain file)."""

    def __init__(self):
        self._bare_repo_path: Path | None = None
        self._ref: str | None = None

    def name(self) -> str:
        return "Lean 4"

    def detect(self, bare_repo_path: Path, ref: str) -> bool:
        """Check for lean-toolchain file at the given ref in the bare repo."""
        content = _read_lean_toolchain(bare_repo_path, ref)
        if content is not None:
            self._bare_repo_path = bare_repo_path
            self._ref = ref
            return True
        self._bare_repo_path = None
        self._ref = None
        return False

    def image_name(self) -> str:
        """Return the image name based on the lean-toolchain version.

        For stable/RC versions (v4.X.Y, v4.X.Y-rcK): returns 'lean-v4.X.Y' or 'lean-v4.X.Y-rcK'.
        For nightlies or unrecognized: returns 'lean' (base image with elan only).
        """
        if self._bare_repo_path and self._ref:
            toolchain = _read_lean_toolchain(self._bare_repo_path, self._ref)
            if toolchain:
                version = _parse_lean_version(toolchain)
                if version:
                    return f"lean-{version}"
        return "lean"

    def network_domains(self) -> list[str]:
        return [
            "releases.lean-lang.org",
            "mathlib4.lean-cache.cloud",
            "lakecache.blob.core.windows.net",
        ]
