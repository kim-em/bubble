"""Lean 4 language hook."""

import json
import re
import shlex
import subprocess
from pathlib import Path

import click

from ..git_store import parse_github_url
from ..lean import LEAN_VERSION_RE
from ..runtime.base import ContainerRuntime
from . import GitDependency, Hook

# Allowlist for Lake package names and repo names (prevents path traversal)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_subdir(subdir: str) -> bool:
    """Validate a detected project subdirectory.

    The string flows into shell commands and into paths under /home/user/, so
    each path component must match the same conservative policy used for Lake
    package names. Reject absolute paths, leading/trailing slashes, empty
    components, and any '.' / '..' segments.
    """
    if not subdir or subdir.startswith("/") or subdir.endswith("/"):
        return False
    for part in subdir.split("/"):
        if not part or part in (".", "..") or not _SAFE_NAME_RE.match(part):
            return False
    return True


def _has_lakefile(bare_repo_path: Path, ref: str, subdir: str) -> bool:
    """Confirm a sibling lakefile exists next to a discovered lean-toolchain.

    Filters out vendored/example/doc directories that happen to ship a
    lean-toolchain without being the project root.
    """
    for name in ("lakefile.toml", "lakefile.lean"):
        try:
            subprocess.run(
                ["git", "-C", str(bare_repo_path), "cat-file", "-e", f"{ref}:{subdir}/{name}"],
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return False


def _read_lean_toolchain(bare_repo_path: Path, ref: str, subdir: str = "") -> str | None:
    """Read the lean-toolchain file content from a bare repo at a given ref.

    ``subdir`` is "" for the repo root, or a relative path like "foo" or "foo/bar".
    """
    path = f"{subdir}/lean-toolchain" if subdir else "lean-toolchain"
    try:
        result = subprocess.run(
            ["git", "-C", str(bare_repo_path), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _find_lean_toolchain_subdir(bare_repo_path: Path, ref: str) -> str | None:
    """Search for a non-root `lean-toolchain` file in the bare repo at `ref`.

    Returns the relative directory containing the file, or None if there are
    zero or multiple matches, or if the unique match doesn't sit next to a
    lakefile. The root is handled separately by the caller; this only fires
    on the slow path when root has no lean-toolchain.

    Uses `git ls-tree -z` (NUL-delimited) and streams output, stopping after
    the second match so a huge tree with no Lean project doesn't pay for the
    full listing. NUL-delimited mode also avoids git's quotePath escaping for
    paths with unusual bytes.
    """
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(bare_repo_path), "ls-tree", "-rz", "--name-only", ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        return None

    matches: list[str] = []
    try:
        assert proc.stdout is not None
        buf = b""
        suffix = b"/lean-toolchain"
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                buf += b""
                # Flush whatever's left.
                for entry in buf.split(b"\0"):
                    if entry.endswith(suffix):
                        matches.append(entry.decode("utf-8", "replace"))
                        if len(matches) >= 2:
                            break
                break
            buf += chunk
            parts = buf.split(b"\0")
            buf = parts[-1]  # last fragment may be incomplete
            for entry in parts[:-1]:
                if entry.endswith(suffix):
                    matches.append(entry.decode("utf-8", "replace"))
            if len(matches) >= 2:
                break
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass

    if not matches:
        return None
    if len(matches) >= 2:
        dirs = sorted(m[: -len("/lean-toolchain")] for m in matches)
        click.echo(
            f"Multiple lean-toolchain files found ({', '.join(dirs)}); "
            "cannot auto-detect Lean project subdirectory.",
            err=True,
        )
        return None
    subdir = matches[0][: -len("/lean-toolchain")]
    if not _is_safe_subdir(subdir):
        return None
    if not _has_lakefile(bare_repo_path, ref, subdir):
        return None
    return subdir


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

    if LEAN_VERSION_RE.fullmatch(version):
        return version
    return None


def _parse_git_dependencies(
    bare_repo_path: Path, ref: str, subdir: str = ""
) -> list[GitDependency]:
    """Parse git dependencies from lake-manifest.json in the bare repo."""
    path = f"{subdir}/lake-manifest.json" if subdir else "lake-manifest.json"
    try:
        result = subprocess.run(
            ["git", "-C", str(bare_repo_path), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
        manifest = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return []

    deps = []
    for pkg in manifest.get("packages", []):
        if pkg.get("type") != "git":
            continue
        url = pkg.get("url", "")
        name = pkg.get("name", "")
        rev = pkg.get("rev", "")
        org_repo = parse_github_url(url)
        if not org_repo:
            continue  # Skip non-GitHub deps
        # Validate name and rev to prevent path traversal and option injection
        if not name or not _SAFE_NAME_RE.match(name):
            continue
        if not rev or not re.match(r"^[0-9a-f]{40}$", rev):
            continue
        deps.append(
            GitDependency(
                name=name,
                url=url,
                rev=rev,
                sub_dir=pkg.get("subDir"),
                org_repo=org_repo,
            )
        )
    return deps


class LeanHook(Hook):
    """Hook for Lean 4 projects (detected by lean-toolchain file)."""

    def __init__(self):
        self._toolchain: str | None = None
        self._needs_cache: bool = False
        self._is_lean4: bool = False
        self._git_deps: list[GitDependency] = []
        self._subdir: str = ""

    def name(self) -> str:
        return "Lean 4"

    def detect(self, bare_repo_path: Path, ref: str) -> bool:
        """Check for lean-toolchain file at the given ref in the bare repo.

        Tries the repo root first (the common case). If that fails, scans the
        tree for a single non-root `lean-toolchain` file and uses its
        directory. Bails out (returns False) when there are multiple matches.
        """
        content = _read_lean_toolchain(bare_repo_path, ref)
        subdir = ""
        if content is None:
            found = _find_lean_toolchain_subdir(bare_repo_path, ref)
            if found is not None:
                content = _read_lean_toolchain(bare_repo_path, ref, found)
                if content is not None:
                    subdir = found

        if content is not None:
            self._toolchain = content
            self._subdir = subdir
            self._is_lean4 = bare_repo_path.name == "lean4.git"
            if self._is_lean4:
                self._git_deps = []
                self._needs_cache = False
            else:
                self._git_deps = _parse_git_dependencies(bare_repo_path, ref, subdir)
                self._needs_cache = bare_repo_path.name == "mathlib4.git" or any(
                    d.name == "mathlib" for d in self._git_deps
                )
            return True
        self._toolchain = None
        self._subdir = ""
        self._needs_cache = False
        self._is_lean4 = False
        self._git_deps = []
        return False

    def project_subdir(self) -> str:
        return self._subdir

    def image_name(self) -> str:
        """Return the image name based on the lean-toolchain version.

        For stable/RC versions (v4.X.Y, v4.X.Y-rcK): returns 'lean-v4.X.Y' or 'lean-v4.X.Y-rcK'.
        For nightlies or unrecognized: returns 'lean' (base image with elan only).
        """
        if self._toolchain:
            version = _parse_lean_version(self._toolchain)
            if version:
                return f"lean-{version}"
        return "lean"

    def shared_mounts(self) -> list[tuple[str, str, str]]:
        if self._needs_cache:
            return [("mathlib-cache", "/shared/mathlib-cache", "MATHLIB_CACHE_DIR")]
        return []

    def git_dependencies(self) -> list[GitDependency]:
        return self._git_deps

    def workspace_file(self, project_dir: str) -> str | None:
        if self._is_lean4:
            return f"{project_dir}/lean.code-workspace"
        return None

    def post_clone(self, runtime: ContainerRuntime, container: str, project_dir: str):
        """Pre-populate Lake dependencies, then set up auto build command."""
        if self._is_lean4:
            self._setup_lean4_build(runtime, container, project_dir)
            return

        if self._git_deps:
            self._populate_lake_packages(runtime, container, project_dir)

        q_dir = shlex.quote(project_dir)
        if self._needs_cache:
            cmd = f"cd {q_dir} && lake exe cache get && lake build"
            msg = "Mathlib cache download and build will start automatically."
        else:
            cmd = f"cd {q_dir} && lake build"
            msg = "Build will start automatically."
        # Write command for the VS Code extension or shell login hook to pick up
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
        click.echo(msg)

    def _setup_lean4_build(self, runtime: ContainerRuntime, container: str, project_dir: str):
        """Set up auto-build for the lean4 repo itself (cmake is in base image)."""
        q_dir = shlex.quote(project_dir)
        cmd = f"cd {q_dir} && cmake --preset release && make -C build/release -j$(nproc)"
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
        click.echo("Lean 4 build will start automatically.")

    def _populate_lake_packages(self, runtime: ContainerRuntime, container: str, project_dir: str):
        """Clone each dependency into .lake/packages/<name>/ using alternates."""
        q_dir = shlex.quote(project_dir)

        # Create .lake/packages/ directory
        runtime.exec(
            container,
            ["su", "-", "user", "-c", f"mkdir -p {q_dir}/.lake/packages"],
        )

        populated = 0
        for dep in self._git_deps:
            repo_name = dep.org_repo.split("/")[-1]
            # All values are validated (_SAFE_NAME_RE, hex SHA, parse_github_url)
            # but quote everything for defense in depth
            q_bare = shlex.quote(f"/shared/git/{repo_name}.git")
            q_url = shlex.quote(dep.url)
            q_rev = shlex.quote(dep.rev)
            q_pkg = shlex.quote(f"{project_dir}/.lake/packages/{dep.name}")

            try:
                # Clone from mounted bare repo with --reference for alternates.
                # Since source and reference are the same, zero objects are copied.
                # Use -c safe.directory to allow reading the root-owned mounted bare repo
                # (scoped to this command only, not persisted in global gitconfig).
                runtime.exec(
                    container,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"git -c safe.directory={q_bare} clone"
                        f" --reference {q_bare} file://{q_bare} {q_pkg}",
                    ],
                )

                # Fix remote URL to match what the manifest expects
                runtime.exec(
                    container,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"git -C {q_pkg} remote set-url origin {q_url}",
                    ],
                )

                # Checkout the exact revision from the manifest
                # rev is validated as a 40-char hex SHA, so no option injection risk
                runtime.exec(
                    container,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"git -C {q_pkg} checkout {q_rev}",
                    ],
                )

                populated += 1
            except RuntimeError as e:
                # Non-fatal: Lake will clone this dep normally when needed
                click.echo(f"  Warning: could not pre-populate {dep.name}: {e}")

        if populated:
            click.echo(f"  Pre-populated {populated}/{len(self._git_deps)} Lake dependencies.")

    def network_domains(self) -> list[str]:
        return [
            "releases.lean-lang.org",
            "reservoir.lean-lang.org",
            "reservoir.lean-cache.cloud",
            "mathlib4.lean-cache.cloud",
            "lakecache.blob.core.windows.net",
        ]
