"""Language/framework hook system for bubble containers."""

from abc import ABC, abstractmethod
from pathlib import Path

from ..runtime.base import ContainerRuntime


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
