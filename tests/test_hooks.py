"""Tests for the hook system."""

import subprocess

import pytest

from bubble.hooks import discover_hooks, select_hook
from bubble.hooks.lean import LeanHook, _parse_lean_version


@pytest.fixture
def lean_repo(tmp_path):
    """Create a bare git repo with a lean-toolchain file."""
    repo = tmp_path / "test.git"
    subprocess.run(["git", "init", "--bare", str(repo)], capture_output=True, check=True)

    # Create a temporary working copy to add a file
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(repo), str(work)], capture_output=True, check=True)
    (work / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n")
    subprocess.run(["git", "-C", str(work), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "master"],
        capture_output=True,
        check=True,
    )
    return repo


@pytest.fixture
def non_lean_repo(tmp_path):
    """Create a bare git repo without a lean-toolchain file."""
    repo = tmp_path / "other.git"
    subprocess.run(["git", "init", "--bare", str(repo)], capture_output=True, check=True)

    work = tmp_path / "work2"
    subprocess.run(["git", "clone", str(repo), str(work)], capture_output=True, check=True)
    (work / "README.md").write_text("# Hello\n")
    subprocess.run(["git", "-C", str(work), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "master"],
        capture_output=True,
        check=True,
    )
    return repo


@pytest.fixture
def nightly_lean_repo(tmp_path):
    """Create a bare git repo with a nightly lean-toolchain file."""
    repo = tmp_path / "nightly.git"
    subprocess.run(["git", "init", "--bare", str(repo)], capture_output=True, check=True)

    work = tmp_path / "work3"
    subprocess.run(["git", "clone", str(repo), str(work)], capture_output=True, check=True)
    (work / "lean-toolchain").write_text("leanprover/lean4:nightly-2025-01-15\n")
    subprocess.run(["git", "-C", str(work), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "master"],
        capture_output=True,
        check=True,
    )
    return repo


GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def _make_lean_bare_repo(tmp_path, repo_name, work_name, toolchain="leanprover/lean4:v4.16.0\n"):
    """Helper to create a bare git repo with a lean-toolchain file."""
    repo = tmp_path / repo_name
    subprocess.run(["git", "init", "--bare", str(repo)], capture_output=True, check=True)
    work = tmp_path / work_name
    subprocess.run(["git", "clone", str(repo), str(work)], capture_output=True, check=True)
    (work / "lean-toolchain").write_text(toolchain)
    subprocess.run(["git", "-C", str(work), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        capture_output=True, check=True,
        env={**GIT_ENV, "HOME": str(tmp_path)},
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "master"],
        capture_output=True, check=True,
    )
    return repo


@pytest.fixture
def lean4_repo(tmp_path):
    """Create a bare git repo named lean4.git with lean-toolchain."""
    return _make_lean_bare_repo(tmp_path, "lean4.git", "work_lean4")


@pytest.fixture
def mathlib4_repo(tmp_path):
    """Create a bare git repo named mathlib4.git with lean-toolchain."""
    return _make_lean_bare_repo(tmp_path, "mathlib4.git", "work_mathlib4")


class TestParseLeanVersion:
    def test_stable(self):
        assert _parse_lean_version("leanprover/lean4:v4.16.0") == "v4.16.0"

    def test_rc(self):
        assert _parse_lean_version("leanprover/lean4:v4.17.0-rc2") == "v4.17.0-rc2"

    def test_nightly(self):
        assert _parse_lean_version("leanprover/lean4:nightly-2025-01-15") is None

    def test_bare_version(self):
        assert _parse_lean_version("v4.16.0") == "v4.16.0"

    def test_custom(self):
        assert _parse_lean_version("leanprover/lean4:my-branch") is None


class TestLeanHook:
    def test_detect_lean_repo(self, lean_repo):
        hook = LeanHook()
        assert hook.detect(lean_repo, "HEAD") is True

    def test_detect_non_lean_repo(self, non_lean_repo):
        hook = LeanHook()
        assert hook.detect(non_lean_repo, "HEAD") is False

    def test_detect_nonexistent_ref(self, lean_repo):
        hook = LeanHook()
        assert hook.detect(lean_repo, "nonexistent-ref") is False

    def test_name(self):
        hook = LeanHook()
        assert hook.name() == "Lean 4"

    def test_image_name_without_detect(self):
        hook = LeanHook()
        assert hook.image_name() == "lean"

    def test_image_name_stable(self, lean_repo):
        hook = LeanHook()
        hook.detect(lean_repo, "HEAD")
        assert hook.image_name() == "lean-v4.27.0"

    def test_image_name_nightly(self, nightly_lean_repo):
        hook = LeanHook()
        hook.detect(nightly_lean_repo, "HEAD")
        assert hook.image_name() == "lean"

    def test_network_domains(self):
        hook = LeanHook()
        assert "releases.lean-lang.org" in hook.network_domains()


class TestLean4Detection:
    def test_lean4_repo_detected(self, lean4_repo):
        hook = LeanHook()
        hook.detect(lean4_repo, "HEAD")
        assert hook._is_lean4 is True

    def test_lean4_no_cache(self, lean4_repo):
        hook = LeanHook()
        hook.detect(lean4_repo, "HEAD")
        assert hook._needs_cache is False

    def test_mathlib4_not_lean4(self, mathlib4_repo):
        hook = LeanHook()
        hook.detect(mathlib4_repo, "HEAD")
        assert hook._is_lean4 is False
        assert hook._needs_cache is True

    def test_regular_lean_not_lean4(self, lean_repo):
        hook = LeanHook()
        hook.detect(lean_repo, "HEAD")
        assert hook._is_lean4 is False

    def test_post_clone_lean4_writes_cmake_marker(self, lean4_repo, mock_runtime):
        hook = LeanHook()
        hook.detect(lean4_repo, "HEAD")
        hook.post_clone(mock_runtime, "test-container", "/home/user/lean4")
        # Find the exec call that writes the marker
        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        marker_calls = [c for c in exec_calls if ".bubble-fetch-cache" in " ".join(c[2])]
        assert len(marker_calls) == 1
        cmd_str = " ".join(marker_calls[0][2])
        assert "cmake --preset release" in cmd_str

    def test_post_clone_lean4_no_apt_get(self, lean4_repo, mock_runtime):
        """lean4 post_clone should NOT install packages (cmake is in base image)."""
        hook = LeanHook()
        hook.detect(lean4_repo, "HEAD")
        hook.post_clone(mock_runtime, "test-container", "/home/user/lean4")
        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        apt_calls = [c for c in exec_calls if "apt-get" in " ".join(c[2])]
        assert len(apt_calls) == 0

    def test_post_clone_mathlib_writes_cache_get(self, mathlib4_repo, mock_runtime):
        hook = LeanHook()
        hook.detect(mathlib4_repo, "HEAD")
        hook.post_clone(mock_runtime, "test-container", "/home/user/mathlib4")
        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        marker_calls = [c for c in exec_calls if ".bubble-fetch-cache" in " ".join(c[2])]
        assert len(marker_calls) == 1
        cmd_str = " ".join(marker_calls[0][2])
        assert "lake exe cache get" in cmd_str

    def test_workspace_file_lean4(self, lean4_repo):
        hook = LeanHook()
        hook.detect(lean4_repo, "HEAD")
        assert hook.workspace_file("/home/user/lean4") == "/home/user/lean4/lean.code-workspace"

    def test_workspace_file_non_lean4(self, lean_repo):
        hook = LeanHook()
        hook.detect(lean_repo, "HEAD")
        assert hook.workspace_file("/home/user/project") is None

    def test_workspace_file_mathlib4(self, mathlib4_repo):
        hook = LeanHook()
        hook.detect(mathlib4_repo, "HEAD")
        assert hook.workspace_file("/home/user/mathlib4") is None


class TestSelectHook:
    def test_selects_lean_for_lean_repo(self, lean_repo):
        hook = select_hook(lean_repo, "HEAD")
        assert hook is not None
        assert hook.name() == "Lean 4"

    def test_returns_none_for_non_lean_repo(self, non_lean_repo):
        hook = select_hook(non_lean_repo, "HEAD")
        assert hook is None


class TestDiscoverHooks:
    def test_returns_list(self):
        hooks = discover_hooks()
        assert isinstance(hooks, list)
        assert len(hooks) >= 1
        assert any(h.name() == "Lean 4" for h in hooks)
