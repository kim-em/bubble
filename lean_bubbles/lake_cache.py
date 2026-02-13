"""Shared .lake cache management.

Manages a shared cache of .olean files keyed by repo + toolchain version.
Containers mount this read-only and can populate it after building.

Note: Lake is planning native shared cache support. When available,
lean-bubbles should integrate with it instead of this custom solution.
"""

import subprocess
from pathlib import Path

from .config import LAKE_CACHE_DIR
from .runtime.base import ContainerRuntime


def cache_key(repo_short: str, toolchain: str) -> str:
    """Generate a cache key from repo and toolchain."""
    # Sanitize toolchain string for filesystem use
    safe_toolchain = toolchain.replace("/", "-").replace(":", "-")
    return f"{repo_short}-{safe_toolchain}"


def cache_path(repo_short: str, toolchain: str) -> Path:
    """Get the cache directory for a repo+toolchain combination."""
    return LAKE_CACHE_DIR / cache_key(repo_short, toolchain)


def cache_exists(repo_short: str, toolchain: str) -> bool:
    """Check if a cache exists for this repo+toolchain."""
    p = cache_path(repo_short, toolchain)
    return p.exists() and any(p.iterdir())


def get_toolchain_from_container(runtime: ContainerRuntime, container: str,
                                  project_dir: str) -> str:
    """Read the lean-toolchain file from a container's project."""
    try:
        return runtime.exec(container, [
            "su", "-", "lean", "-c",
            f"cat {project_dir}/lean-toolchain",
        ]).strip()
    except Exception:
        return "unknown"


def populate_cache_from_container(runtime: ContainerRuntime, container: str,
                                   project_dir: str, repo_short: str):
    """Extract .lake cache from a container and save to shared cache.

    This copies the build artifacts from a container into the host-side
    shared cache directory for future containers to use.
    """
    toolchain = get_toolchain_from_container(runtime, container, project_dir)
    dest = cache_path(repo_short, toolchain)
    dest.mkdir(parents=True, exist_ok=True)

    # Archive the .lake directory from the container
    try:
        runtime.exec(container, [
            "su", "-", "lean", "-c",
            f"cd {project_dir} && tar cf /tmp/lake-cache.tar .lake/",
        ])
        # Copy archive out of container using incus file pull
        subprocess.run(
            ["incus", "file", "pull", f"{container}/tmp/lake-cache.tar",
             str(dest / "lake-cache.tar")],
            check=True, capture_output=True,
        )
        # Extract on host
        subprocess.run(
            ["tar", "xf", str(dest / "lake-cache.tar"), "-C", str(dest)],
            check=True, capture_output=True,
        )
        (dest / "lake-cache.tar").unlink()
    except Exception as e:
        print(f"Warning: failed to populate lake cache: {e}")


def inject_cache_into_container(runtime: ContainerRuntime, container: str,
                                 project_dir: str, repo_short: str):
    """Inject cached .lake directory into a container if available."""
    toolchain = get_toolchain_from_container(runtime, container, project_dir)
    src = cache_path(repo_short, toolchain)

    if not src.exists() or not (src / ".lake").exists():
        return False

    try:
        # Archive on host
        subprocess.run(
            ["tar", "cf", str(src / "lake-cache.tar"), "-C", str(src), ".lake/"],
            check=True, capture_output=True,
        )
        # Push into container
        subprocess.run(
            ["incus", "file", "push", str(src / "lake-cache.tar"),
             f"{container}/tmp/lake-cache.tar"],
            check=True, capture_output=True,
        )
        (src / "lake-cache.tar").unlink()
        # Extract in container
        runtime.exec(container, [
            "su", "-", "lean", "-c",
            f"cd {project_dir} && tar xf /tmp/lake-cache.tar && rm /tmp/lake-cache.tar",
        ])
        return True
    except Exception as e:
        print(f"Warning: failed to inject lake cache: {e}")
        return False
