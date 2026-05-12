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
    """Check for a sibling lakefile alongside a discovered lean-toolchain.

    Filters out vendored/example/doc directories that happen to ship a
    `lean-toolchain` without being a buildable Lean project — e.g. a Python
    repo with `vendor/lean-toolchain` shouldn't be mistaken for a Lean repo.
    """
    prefix = f"{subdir}/" if subdir else ""
    for name in ("lakefile.toml", "lakefile.lean"):
        try:
            subprocess.run(
                ["git", "-C", str(bare_repo_path), "cat-file", "-e", f"{ref}:{prefix}{name}"],
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


def _find_lean_toolchain_subdirs(bare_repo_path: Path, ref: str) -> list[str]:
    """Return every directory containing a `lean-toolchain` file at `ref`.

    Empty string represents the repo root. Uses `git ls-tree -rz` so paths
    with unusual bytes aren't quoted, and streams output through a single
    decode/split pass. Unsafe path components (traversal, control chars,
    non-ASCII) are silently dropped — they'd be unsafe to plumb into shell
    commands later.
    """
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(bare_repo_path), "ls-tree", "-rz", "--name-only", ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        return []

    subdirs: list[str] = []
    suffix = b"/lean-toolchain"
    try:
        assert proc.stdout is not None
        data = proc.stdout.read()
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    for entry in data.split(b"\0"):
        if entry == b"lean-toolchain":
            subdirs.append("")
        elif entry.endswith(suffix):
            try:
                subdir = entry[: -len(suffix)].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if _is_safe_subdir(subdir):
                subdirs.append(subdir)
    return subdirs


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
        self._multi_project: bool = False
        self._notices: list[str] = []

    def name(self) -> str:
        return "Lean 4"

    def _reset_state(self) -> None:
        self._toolchain = None
        self._subdir = ""
        self._needs_cache = False
        self._is_lean4 = False
        self._git_deps = []
        self._multi_project = False
        self._notices = []

    def detect(self, bare_repo_path: Path, ref: str) -> bool:
        """Fire if there is any `lean-toolchain` file at the given ref.

        The repo root is preferred when present. For repos with one or more
        `lean-toolchain` files in subdirectories, the hook still fires (so
        elan + the VS Code extension end up in the bubble), but VS Code
        always opens at the repo root. When multiple files exist the
        auto-build is skipped and a notice is emitted; when their contents
        disagree the plain `lean` image is used and elan installs toolchains
        on demand.
        """
        self._reset_state()
        root_content = _read_lean_toolchain(bare_repo_path, ref)

        if root_content is not None:
            # Root lean-toolchain wins — treat the repo as a single-root
            # project even if additional lean-toolchain files exist deeper
            # in the tree. Subdir copies in real repos are almost always
            # vendored/test fixtures (e.g. lean4 itself ships them under
            # tests/), so demoting to multi-project would be wrong.
            self._toolchain = root_content
            self._subdir = ""
            self._configure_for_single_project(bare_repo_path, ref, "")
            return True

        # Only count subdirs that look like real Lean projects (sibling
        # lakefile). This filters out e.g. a Python repo with a vendored
        # `lean-toolchain` that has no lakefile next to it.
        candidates = [
            s
            for s in _find_lean_toolchain_subdirs(bare_repo_path, ref)
            if _has_lakefile(bare_repo_path, ref, s)
        ]
        if not candidates:
            return False

        if len(candidates) == 1:
            subdir = candidates[0]
            content = _read_lean_toolchain(bare_repo_path, ref, subdir)
            if content is None:
                return False
            self._toolchain = content
            self._subdir = subdir
            self._configure_for_single_project(bare_repo_path, ref, subdir)
            return True

        # Multiple buildable Lean projects across subdirectories.
        self._multi_project = True
        self._subdir = ""  # we don't pick one for the auto-build
        contents: list[str] = []
        for s in candidates:
            c = _read_lean_toolchain(bare_repo_path, ref, s)
            if c is not None:
                contents.append(c)
        unique = sorted(set(contents))
        dirs_label = ", ".join(sorted(candidates))
        if len(unique) == 1:
            self._toolchain = contents[0]
            self._notices.append(
                f"Multiple Lean projects detected ({dirs_label}); skipping auto-build."
                " Run `lake build` in your project's subdirectory."
            )
        else:
            self._toolchain = None  # forces image_name() to plain `lean`
            versions_label = ", ".join(unique)
            self._notices.append(
                f"Multiple Lean toolchains detected across {dirs_label}: {versions_label}."
                " Using the base `lean` image; elan will install each toolchain"
                " on demand the first time you run `lake build` in a subdirectory."
            )
        self._is_lean4 = False
        self._git_deps = []
        self._needs_cache = False
        return True

    def _configure_for_single_project(self, bare_repo_path: Path, ref: str, subdir: str) -> None:
        """Populate is_lean4 / git_deps / needs_cache for a single-project repo."""
        self._is_lean4 = bare_repo_path.name == "lean4.git"
        if self._is_lean4:
            self._git_deps = []
            self._needs_cache = False
        else:
            self._git_deps = _parse_git_dependencies(bare_repo_path, ref, subdir)
            self._needs_cache = bare_repo_path.name == "mathlib4.git" or any(
                d.name == "mathlib" for d in self._git_deps
            )

    def notices(self) -> list[str]:
        return list(self._notices)

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
        """Pre-populate Lake dependencies, then set up auto build command.

        ``project_dir`` is always the repo root. The hook's stored ``_subdir``
        controls where ``lake build`` actually runs. For multi-project repos
        the auto-build is skipped entirely.
        """
        if self._multi_project:
            return

        if self._is_lean4:
            self._setup_lean4_build(runtime, container, project_dir)
            return

        build_dir = f"{project_dir}/{self._subdir}" if self._subdir else project_dir

        if self._git_deps:
            self._populate_lake_packages(runtime, container, build_dir)

        q_dir = shlex.quote(build_dir)
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
