"""Shared .lake cache management.

Manages a shared cache of .olean files keyed by repo + toolchain version.
Containers mount this read-only and can populate it after building.

Note: Lake is planning native shared cache support. When available,
bubble should integrate with it instead of this custom solution.
"""

import shlex
import subprocess
import tarfile
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


def get_toolchain_from_container(
    runtime: ContainerRuntime, container: str, project_dir: str
) -> str:
    """Read the lean-toolchain file from a container's project."""
    try:
        q_dir = shlex.quote(project_dir)
        return runtime.exec(
            container,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cat {q_dir}/lean-toolchain",
            ],
        ).strip()
    except Exception:
        return "unknown"


def _safe_extract_tar(tar_path: Path, dest: Path):
    """Extract a tar archive with safety checks against path traversal.

    Rejects archives containing:
    - Absolute paths
    - Path traversal (..)
    - Symlinks or hardlinks
    """
    dest_resolved = dest.resolve()
    with tarfile.open(tar_path) as tf:
        for member in tf.getmembers():
            # Reject absolute paths
            if member.name.startswith("/"):
                raise ValueError(f"Unsafe tar member (absolute path): {member.name}")
            # Reject path traversal
            if ".." in member.name.split("/"):
                raise ValueError(f"Unsafe tar member (path traversal): {member.name}")
            # Reject symlinks and hardlinks
            if member.issym() or member.islnk():
                raise ValueError(f"Unsafe tar member (symlink/hardlink): {member.name}")
            # Reject device nodes and FIFOs
            if member.isdev() or member.isfifo():
                raise ValueError(f"Unsafe tar member (device/fifo): {member.name}")
            # Verify extraction target stays within dest
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest_resolved)):
                raise ValueError(f"Unsafe tar member (escapes dest): {member.name}")
        # All members validated, extract
        tf.extractall(dest)


def populate_cache_from_container(
    runtime: ContainerRuntime, container: str, project_dir: str, repo_short: str
):
    """Extract .lake cache from a container and save to shared cache.

    This copies the build artifacts from a container into the host-side
    shared cache directory for future containers to use.
    """
    toolchain = get_toolchain_from_container(runtime, container, project_dir)
    dest = cache_path(repo_short, toolchain)
    dest.mkdir(parents=True, exist_ok=True)

    tar_file = dest / "lake-cache.tar"
    try:
        # Archive the .lake directory from the container
        q_dir = shlex.quote(project_dir)
        runtime.exec(
            container,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd {q_dir} && tar cf /tmp/lake-cache.tar .lake/",
            ],
        )
        # Copy archive out of container
        subprocess.run(
            ["incus", "file", "pull", f"{container}/tmp/lake-cache.tar", str(tar_file)],
            check=True,
            capture_output=True,
        )
        # Safe extraction on host
        _safe_extract_tar(tar_file, dest)
    except ValueError as e:
        print(f"Security: refusing to extract lake cache: {e}")
    except Exception as e:
        print(f"Warning: failed to populate lake cache: {e}")
    finally:
        if tar_file.exists():
            tar_file.unlink()


def inject_cache_into_container(
    runtime: ContainerRuntime, container: str, project_dir: str, repo_short: str
):
    """Inject cached .lake directory into a container if available."""
    toolchain = get_toolchain_from_container(runtime, container, project_dir)
    src = cache_path(repo_short, toolchain)

    if not src.exists() or not (src / ".lake").exists():
        return False

    tar_file = src / "lake-cache.tar"
    try:
        # Archive on host (this is our own cache, trusted)
        subprocess.run(
            ["tar", "cf", str(tar_file), "-C", str(src), ".lake/"],
            check=True,
            capture_output=True,
        )
        # Push into container
        subprocess.run(
            ["incus", "file", "push", str(tar_file), f"{container}/tmp/lake-cache.tar"],
            check=True,
            capture_output=True,
        )
        # Extract in container
        q_dir = shlex.quote(project_dir)
        runtime.exec(
            container,
            [
                "su",
                "-",
                "lean",
                "-c",
                f"cd {q_dir} && tar xf /tmp/lake-cache.tar && rm /tmp/lake-cache.tar",
            ],
        )
        return True
    except Exception as e:
        print(f"Warning: failed to inject lake cache: {e}")
        return False
    finally:
        if tar_file.exists():
            tar_file.unlink()
