"""Tests for the hook system."""

import shutil
import subprocess

import pytest

from bubble.hooks import discover_hooks, select_hook
from bubble.hooks.lean import LeanHook, _parse_lean_version
from bubble.hooks.python import PythonHook

GIT = shutil.which("git")
if GIT is None:
    pytest.skip("git not available", allow_module_level=True)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _make_hook_repo(tmp_path, work_name, files):
    """Helper to create a bare git repo with given files for hook testing."""
    repo = tmp_path / f"{work_name}.git"
    subprocess.run([GIT, "init", "--bare", str(repo)], capture_output=True, check=True)
    work = tmp_path / work_name
    subprocess.run([GIT, "clone", str(repo), str(work)], capture_output=True, check=True)
    for name, content in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run([GIT, "-C", str(work), "add", "."], capture_output=True, check=True)
    subprocess.run(
        [GIT, "-C", str(work), "commit", "-m", "init"],
        capture_output=True,
        check=True,
        env={**GIT_ENV, "HOME": str(tmp_path)},
    )
    subprocess.run(
        [GIT, "-C", str(work), "push", "origin", "HEAD"],
        capture_output=True,
        check=True,
    )
    return repo


@pytest.fixture
def lean_repo(tmp_path):
    """Create a bare git repo with a lean-toolchain file."""
    return _make_hook_repo(tmp_path, "work", {"lean-toolchain": "leanprover/lean4:v4.27.0\n"})


@pytest.fixture
def non_lean_repo(tmp_path):
    """Create a bare git repo without a lean-toolchain file."""
    return _make_hook_repo(tmp_path, "work2", {"README.md": "# Hello\n"})


@pytest.fixture
def nightly_lean_repo(tmp_path):
    """Create a bare git repo with a nightly lean-toolchain file."""
    return _make_hook_repo(
        tmp_path, "work3", {"lean-toolchain": "leanprover/lean4:nightly-2025-01-15\n"}
    )


def _make_lean_bare_repo(tmp_path, repo_name, work_name, toolchain="leanprover/lean4:v4.16.0\n"):
    """Helper to create a bare git repo with a lean-toolchain file."""
    repo = tmp_path / repo_name
    subprocess.run([GIT, "init", "--bare", str(repo)], capture_output=True, check=True)
    work = tmp_path / work_name
    subprocess.run([GIT, "clone", str(repo), str(work)], capture_output=True, check=True)
    (work / "lean-toolchain").write_text(toolchain)
    subprocess.run([GIT, "-C", str(work), "add", "."], capture_output=True, check=True)
    subprocess.run(
        [GIT, "-C", str(work), "commit", "-m", "init"],
        capture_output=True,
        check=True,
        env={**GIT_ENV, "HOME": str(tmp_path)},
    )
    subprocess.run(
        [GIT, "-C", str(work), "push", "origin", "HEAD"],
        capture_output=True,
        check=True,
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
        assert "cd" in cmd_str
        assert "/home/user/lean4" in cmd_str

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
        assert "cd" in cmd_str
        assert "/home/user/mathlib4" in cmd_str

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


@pytest.fixture
def subdir_lean_repo(tmp_path):
    """Create a bare git repo with lean-toolchain in a subdirectory."""
    return _make_hook_repo(
        tmp_path,
        "subdir-work",
        {
            "README.md": "# Root\n",
            "myproj/lean-toolchain": "leanprover/lean4:v4.20.0\n",
            "myproj/lakefile.toml": "name = 'myproj'\n",
        },
    )


@pytest.fixture
def nested_subdir_lean_repo(tmp_path):
    """Create a bare git repo with lean-toolchain in a nested subdirectory."""
    return _make_hook_repo(
        tmp_path,
        "nested-work",
        {
            "docs/README.md": "# Docs\n",
            "src/lean/lean-toolchain": "leanprover/lean4:v4.21.0\n",
            "src/lean/lakefile.lean": "package myproj\n",
        },
    )


@pytest.fixture
def vendor_only_lean_repo(tmp_path):
    """Repo whose only lean-toolchain lives in a directory with no lakefile.

    Detection should NOT fire — this is the false-positive guard: a Python
    repo could plausibly vendor a lean-toolchain file, and we don't want
    that to inject the Lean image and a failing `lake build`.
    """
    return _make_hook_repo(
        tmp_path,
        "vendor-only",
        {
            "README.md": "# Root\n",
            "vendor/lean-toolchain": "leanprover/lean4:v4.20.0\n",
        },
    )


@pytest.fixture
def multi_identical_lean_repo(tmp_path):
    """Multiple lean-toolchain files in subdirs, all with identical content."""
    return _make_hook_repo(
        tmp_path,
        "multi-same",
        {
            "a/lean-toolchain": "leanprover/lean4:v4.20.0\n",
            "a/lakefile.toml": "name = 'a'\n",
            "b/lean-toolchain": "leanprover/lean4:v4.20.0\n",
            "b/lakefile.toml": "name = 'b'\n",
        },
    )


@pytest.fixture
def multi_versions_lean_repo(tmp_path):
    """Multiple lean-toolchain files in subdirs with different versions."""
    return _make_hook_repo(
        tmp_path,
        "multi-versions",
        {
            "a/lean-toolchain": "leanprover/lean4:v4.20.0\n",
            "a/lakefile.toml": "name = 'a'\n",
            "b/lean-toolchain": "leanprover/lean4:v4.21.0\n",
            "b/lakefile.toml": "name = 'b'\n",
        },
    )


@pytest.fixture
def root_and_subdir_lean_repo(tmp_path):
    """Root lean-toolchain plus extra copies in subdirs — root wins."""
    return _make_hook_repo(
        tmp_path,
        "root-and-sub",
        {
            "lean-toolchain": "leanprover/lean4:v4.19.0\n",
            "vendor/lean-toolchain": "leanprover/lean4:v4.10.0\n",
        },
    )


class TestSubdirDetection:
    def test_detect_subdir_lean_repo(self, subdir_lean_repo):
        hook = LeanHook()
        assert hook.detect(subdir_lean_repo, "HEAD") is True
        assert hook._subdir == "myproj"
        assert hook.image_name() == "lean-v4.20.0"
        assert hook._multi_project is False
        assert hook.notices() == []

    def test_detect_nested_subdir(self, nested_subdir_lean_repo):
        hook = LeanHook()
        assert hook.detect(nested_subdir_lean_repo, "HEAD") is True
        assert hook._subdir == "src/lean"
        assert hook.image_name() == "lean-v4.21.0"

    def test_vendor_only_not_detected(self, vendor_only_lean_repo):
        """A non-root lean-toolchain without a sibling lakefile is ignored.

        Guards against false positives like a Python repo with a vendored
        lean-toolchain ending up on the Lean image with a failing auto-build.
        """
        hook = LeanHook()
        assert hook.detect(vendor_only_lean_repo, "HEAD") is False
        assert hook._subdir == ""

    def test_root_wins_over_subdir(self, root_and_subdir_lean_repo):
        hook = LeanHook()
        assert hook.detect(root_and_subdir_lean_repo, "HEAD") is True
        assert hook._subdir == ""
        assert hook._multi_project is False
        assert hook.image_name() == "lean-v4.19.0"

    def test_root_wins_even_with_buildable_subdir(self, tmp_path):
        """Root lean-toolchain takes precedence even if a buildable subdir exists.

        Subdir lean-toolchain files in real repos (e.g. lean4 itself) are
        almost always test/vendor fixtures, so root is canonical.
        """
        repo = _make_hook_repo(
            tmp_path,
            "root-plus-buildable-sub",
            {
                "lean-toolchain": "leanprover/lean4:v4.19.0\n",
                "lakefile.toml": "name = 'main'\n",
                "sub/lean-toolchain": "leanprover/lean4:v4.20.0\n",
                "sub/lakefile.toml": "name = 'sub'\n",
            },
        )
        hook = LeanHook()
        assert hook.detect(repo, "HEAD") is True
        assert hook._subdir == ""
        assert hook._multi_project is False
        assert hook.image_name() == "lean-v4.19.0"
        assert hook.notices() == []

    def test_root_repo_subdir_empty(self, lean_repo):
        hook = LeanHook()
        hook.detect(lean_repo, "HEAD")
        assert hook._subdir == ""

    def test_state_cleared_on_redetect_failure(self, subdir_lean_repo, non_lean_repo):
        hook = LeanHook()
        hook.detect(subdir_lean_repo, "HEAD")
        assert hook._subdir == "myproj"
        assert hook.detect(non_lean_repo, "HEAD") is False
        assert hook._subdir == ""
        assert hook._multi_project is False
        assert hook.notices() == []


class TestMultiProject:
    def test_identical_toolchains_pick_image_skip_build(
        self, multi_identical_lean_repo, mock_runtime
    ):
        hook = LeanHook()
        assert hook.detect(multi_identical_lean_repo, "HEAD") is True
        assert hook._multi_project is True
        assert hook.image_name() == "lean-v4.20.0"
        assert hook._subdir == ""
        notes = hook.notices()
        assert len(notes) == 1
        assert "Multiple Lean projects" in notes[0]
        assert "a" in notes[0] and "b" in notes[0]

        hook.post_clone(mock_runtime, "c", "/home/user/repo")
        marker_calls = [
            c
            for c in mock_runtime.calls
            if c[0] == "exec" and ".bubble-fetch-cache" in " ".join(c[2])
        ]
        assert marker_calls == []

    def test_differing_toolchains_use_plain_lean(self, multi_versions_lean_repo):
        hook = LeanHook()
        assert hook.detect(multi_versions_lean_repo, "HEAD") is True
        assert hook._multi_project is True
        assert hook.image_name() == "lean"
        notes = hook.notices()
        assert len(notes) == 1
        assert "Multiple Lean toolchains" in notes[0]
        assert "v4.20.0" in notes[0] and "v4.21.0" in notes[0]
        assert "elan will install" in notes[0]

    def test_subdir_build_dir(self, subdir_lean_repo, mock_runtime):
        """Single-subdir auto-build runs in the subdir, not the repo root."""
        hook = LeanHook()
        hook.detect(subdir_lean_repo, "HEAD")
        hook.post_clone(mock_runtime, "c", "/home/user/repo")
        marker_calls = [
            c
            for c in mock_runtime.calls
            if c[0] == "exec" and ".bubble-fetch-cache" in " ".join(c[2])
        ]
        assert len(marker_calls) == 1
        assert "/home/user/repo/myproj" in " ".join(marker_calls[0][2])


class TestSafeSubdir:
    def test_safe_names(self):
        from bubble.hooks.lean import _is_safe_subdir

        assert _is_safe_subdir("myproj")
        assert _is_safe_subdir("src/lean")
        assert _is_safe_subdir("a.b-c_d/e1")

    def test_rejects_traversal(self):
        from bubble.hooks.lean import _is_safe_subdir

        assert not _is_safe_subdir("..")
        assert not _is_safe_subdir("../etc")
        assert not _is_safe_subdir("foo/../bar")
        assert not _is_safe_subdir(".")
        assert not _is_safe_subdir("./foo")

    def test_rejects_absolute_and_edge_cases(self):
        from bubble.hooks.lean import _is_safe_subdir

        assert not _is_safe_subdir("")
        assert not _is_safe_subdir("/abs")
        assert not _is_safe_subdir("trailing/")
        assert not _is_safe_subdir("foo//bar")
        assert not _is_safe_subdir("foo bar")
        assert not _is_safe_subdir("foo;bar")
        assert not _is_safe_subdir("foo\nbar")
        assert not _is_safe_subdir("café")


@pytest.fixture
def python_repo(tmp_path):
    """Create a bare git repo with a pyproject.toml file."""
    toml = '[project]\nname = "example"\n'
    return _make_hook_repo(tmp_path, "pyproject", {"pyproject.toml": toml})


class TestPythonHook:
    def test_detect_python_repo(self, python_repo):
        hook = PythonHook()
        assert hook.detect(python_repo, "HEAD") is True

    def test_detect_non_python_repo(self, non_lean_repo):
        hook = PythonHook()
        assert hook.detect(non_lean_repo, "HEAD") is False

    def test_detect_nonexistent_ref(self, python_repo):
        hook = PythonHook()
        assert hook.detect(python_repo, "nonexistent-ref") is False

    def test_name(self):
        hook = PythonHook()
        assert hook.name() == "Python"

    def test_image_name(self):
        hook = PythonHook()
        assert hook.image_name() == "python"

    def test_network_domains(self):
        hook = PythonHook()
        domains = hook.network_domains()
        assert "pypi.org" in domains
        assert "files.pythonhosted.org" in domains

    def test_post_clone_writes_uv_sync(self, python_repo, mock_runtime):
        hook = PythonHook()
        hook.detect(python_repo, "HEAD")
        hook.post_clone(mock_runtime, "test-container", "/home/user/project")
        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        marker_calls = [c for c in exec_calls if ".bubble-fetch-cache" in " ".join(c[2])]
        assert len(marker_calls) == 1
        cmd_str = " ".join(marker_calls[0][2])
        assert "uv sync" in cmd_str
        assert "cd" in cmd_str
        assert "/home/user/project" in cmd_str


class TestSelectHook:
    def test_selects_lean_for_lean_repo(self, lean_repo):
        hook = select_hook(lean_repo, "HEAD")
        assert hook is not None
        assert hook.name() == "Lean 4"

    def test_selects_python_for_python_repo(self, python_repo):
        hook = select_hook(python_repo, "HEAD")
        assert hook is not None
        assert hook.name() == "Python"

    def test_returns_none_for_non_lean_repo(self, non_lean_repo):
        hook = select_hook(non_lean_repo, "HEAD")
        assert hook is None


class TestDiscoverHooks:
    def test_returns_list(self):
        hooks = discover_hooks()
        assert isinstance(hooks, list)
        assert len(hooks) >= 2
        assert any(h.name() == "Lean 4" for h in hooks)
        assert any(h.name() == "Python" for h in hooks)
