"""Python language hook."""

import shlex
import subprocess
from pathlib import Path

import click

from ..runtime.base import ContainerRuntime
from . import Hook


class PythonHook(Hook):
    """Hook for Python projects (detected by pyproject.toml file)."""

    def name(self) -> str:
        return "Python"

    def detect(self, bare_repo_path: Path, ref: str) -> bool:
        """Check for pyproject.toml file at the given ref in the bare repo."""
        try:
            subprocess.run(
                ["git", "-C", str(bare_repo_path), "show", f"{ref}:pyproject.toml"],
                capture_output=True,
                text=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def image_name(self) -> str:
        return "python"

    def post_clone(self, runtime: ContainerRuntime, container: str, project_dir: str):
        """Set up auto-sync for Python projects using uv."""
        q_dir = shlex.quote(project_dir)
        cmd = f"cd {q_dir} && uv sync"
        runtime.exec(
            container,
            [
                "su",
                "-",
                "user",
                "-c",
                f"printf '%s' {shlex.quote(cmd)} > ~/.bubble-fetch-cache",
            ],
        )
        click.echo("Python dependency sync will start automatically.")

    def network_domains(self) -> list[str]:
        return [
            "pypi.org",
            "files.pythonhosted.org",
        ]
