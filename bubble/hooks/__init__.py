"""Language/framework hook system for bubble containers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..runtime.base import ContainerRuntime


@dataclass
class GitDependency:
    """A git dependency to pre-populate in the container via alternates."""

    name: str  # Lake package name (e.g., "Qq")
    url: str  # Original GitHub URL
    rev: str  # Commit SHA from manifest
    sub_dir: str | None  # Subdirectory within repo (rare)
    org_repo: str  # "owner/repo" extracted from URL


class Hook(ABC):
    """Base class for language/framework hooks.

    Hooks inspect a repo and decide if they're relevant, then provide
    the appropriate image and optional post-setup configuration.
    """

    @abstractmethod
    def name(self) -> str:
        """Human-readable hook name, e.g. 'Lean 4'."""

    @abstractmethod
    def detect(self, bare_repo_path: Path, ref: str) -> bool:
        """Check if this hook applies to the repo at the given ref.

        Runs on the host against the bare repo (no container needed).
        """

    @abstractmethod
    def image_name(self) -> str:
        """Return the base image name to use, e.g. 'lean'."""

    def post_clone(self, runtime: ContainerRuntime, container: str, project_dir: str):
        """Optional: run after the repo is cloned inside the container."""

    def network_domains(self) -> list[str]:
        """Additional domains this hook needs in the network allowlist."""
        return []

    def shared_mounts(self) -> list[tuple[str, str, str]]:
        """Host directories shared (writable) across containers.

        Returns list of (host_dir_name, container_path, env_var) tuples.
        host_dir_name is created under ~/.bubble/ on the host.
        container_path is the mount point inside the container.
        env_var is set to container_path in the user's environment.
        """
        return []

    def git_dependencies(self) -> list[GitDependency]:
        """Git repos this project depends on, for pre-population via alternates.

        Returns list of GitDependency objects parsed from the project manifest.
        Only populated after detect() returns True.
        """
        return []


def discover_hooks() -> list[Hook]:
    """Return all registered hooks in priority order."""
    from .lean import LeanHook

    return [LeanHook()]


def select_hook(bare_repo_path: Path, ref: str) -> Hook | None:
    """Run detection on all hooks. Return first match or None."""
    for hook in discover_hooks():
        if hook.detect(bare_repo_path, ref):
            return hook
    return None
