"""Tests for the hook system."""

import subprocess

import pytest

from bubble.hooks import discover_hooks, select_hook
from bubble.hooks.lean import LeanHook


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
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
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
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "master"],
        capture_output=True,
        check=True,
    )
    return repo


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

    def test_image_name(self):
        hook = LeanHook()
        assert hook.image_name() == "bubble-lean"

    def test_vscode_extensions(self):
        hook = LeanHook()
        assert "leanprover.lean4" in hook.vscode_extensions()

    def test_network_domains(self):
        hook = LeanHook()
        assert "releases.lean-lang.org" in hook.network_domains()


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
