"""Lean 4 language hook."""

import subprocess
from pathlib import Path

from . import Hook


class LeanHook(Hook):
    """Hook for Lean 4 projects (detected by lean-toolchain file)."""

    def name(self) -> str:
        return "Lean 4"

    def detect(self, bare_repo_path: Path, ref: str) -> bool:
        """Check for lean-toolchain file at the given ref in the bare repo."""
        try:
            subprocess.run(
                ["git", "-C", str(bare_repo_path), "show", f"{ref}:lean-toolchain"],
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def image_name(self) -> str:
        return "lean"

    def network_domains(self) -> list[str]:
        return ["releases.lean-lang.org"]
