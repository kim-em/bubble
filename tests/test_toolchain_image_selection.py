"""Tests for issue #312: stale mirror / wrong toolchain image selection.

Two fixes are covered:

1. ``refresh_mirror_ref`` refreshes the bare-mirror ref used for toolchain
   detection, so a freshly pushed ``lean-toolchain`` bump is seen on the next
   ``bubble open`` instead of waiting for the hourly mirror refresh.

2. ``detect_and_build_image`` builds the exact ``lean-vX.Y.Z`` image
   synchronously when it is missing and the container will run under the
   network allowlist (so elan never has to download a blocked toolchain inside
   the container). With no network restriction it keeps the fast plain-``lean``
   fallback plus a background build.
"""

import subprocess
from dataclasses import dataclass

import pytest

from bubble import image_management
from bubble.git_store import bare_repo_path, refresh_mirror_ref
from bubble.images import builder as builder_mod


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_origin(path, toolchain):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "lean-toolchain").write_text(toolchain)
    (path / "lakefile.toml").write_text('name = "demo"\n')
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")


def _mirror_from(origin, mirror):
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(origin), str(mirror)],
        check=True,
        capture_output=True,
    )


def _read_toolchain(mirror, ref):
    return subprocess.run(
        ["git", "-C", str(mirror), "show", f"{ref}:lean-toolchain"],
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestRefreshMirrorRef:
    def test_branch_ref_refreshed(self, tmp_data_dir, tmp_path):
        origin = tmp_path / "origin"
        _init_origin(origin, "leanprover/lean4:v4.31.0-rc2\n")
        mirror = bare_repo_path("owner/demo")
        _mirror_from(origin, mirror)
        # Reconfigure the mirror's origin to the local origin path so the
        # refresh fetch reaches it (clone --bare records the same URL already).
        assert _read_toolchain(mirror, "main") == "leanprover/lean4:v4.31.0-rc2"

        # Bump the toolchain upstream after the mirror was taken.
        (origin / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")
        _git(origin, "commit", "-aqm", "bump toolchain")

        # Stale mirror still sees the old toolchain.
        assert _read_toolchain(mirror, "main") == "leanprover/lean4:v4.31.0-rc2"

        refresh_mirror_ref("owner/demo", "branch", "main")

        # After refresh the mirror's branch ref reflects the bump.
        assert _read_toolchain(mirror, "main") == "leanprover/lean4:v4.31.0"

    def test_default_branch_refreshed_for_repo_kind(self, tmp_data_dir, tmp_path):
        origin = tmp_path / "origin"
        _init_origin(origin, "leanprover/lean4:v4.30.0\n")
        mirror = bare_repo_path("owner/demo")
        _mirror_from(origin, mirror)

        (origin / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")
        _git(origin, "commit", "-aqm", "bump")

        refresh_mirror_ref("owner/demo", "repo", "")

        # HEAD (the mirror's default branch) now reflects the bump.
        assert _read_toolchain(mirror, "HEAD") == "leanprover/lean4:v4.31.0"

    def test_pr_kind_is_noop(self, tmp_data_dir, tmp_path):
        origin = tmp_path / "origin"
        _init_origin(origin, "leanprover/lean4:v4.30.0\n")
        mirror = bare_repo_path("owner/demo")
        _mirror_from(origin, mirror)

        (origin / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")
        _git(origin, "commit", "-aqm", "bump")

        # PR refs are fetched separately by the caller; this must not touch the
        # branch ref.
        refresh_mirror_ref("owner/demo", "pr", "5")
        assert _read_toolchain(mirror, "main") == "leanprover/lean4:v4.30.0"

    def test_branch_with_slash_refreshed(self, tmp_data_dir, tmp_path):
        # Branch names with slashes are valid and flow into the refspec.
        origin = tmp_path / "origin"
        _init_origin(origin, "leanprover/lean4:v4.30.0\n")
        _git(origin, "checkout", "-q", "-b", "feature/bump")
        mirror = bare_repo_path("owner/demo")
        _mirror_from(origin, mirror)

        (origin / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")
        _git(origin, "commit", "-aqm", "bump")

        refresh_mirror_ref("owner/demo", "branch", "feature/bump")
        assert _read_toolchain(mirror, "feature/bump") == "leanprover/lean4:v4.31.0"

    def test_missing_mirror_is_noop(self, tmp_data_dir):
        # No exception when the mirror doesn't exist yet.
        refresh_mirror_ref("owner/never-cloned", "branch", "main")


@dataclass
class _Target:
    kind: str
    ref: str


class _FakeHook:
    def name(self):
        return "Lean 4"

    def image_name(self):
        return "lean-v4.31.0"


@pytest.fixture
def patch_hook(monkeypatch):
    monkeypatch.setattr(image_management, "select_hook", lambda ref_path, ref: _FakeHook())


class TestToolchainImageBuildPolicy:
    def test_missing_toolchain_built_synchronously_under_network(
        self, mock_runtime, patch_hook, monkeypatch
    ):
        built = []
        monkeypatch.setattr(
            builder_mod,
            "build_lean_toolchain_image",
            lambda runtime, version: (
                built.append(version),
                mock_runtime._images.add("lean-v4.31.0"),
            ),
            raising=False,
        )
        bg = []
        monkeypatch.setattr(
            image_management,
            "_background_build_lean_toolchain",
            lambda version: bg.append(version),
        )

        hook, image_name = image_management.detect_and_build_image(
            mock_runtime, "/ref", _Target("branch", "main"), restricted_network=True
        )

        assert image_name == "lean-v4.31.0"
        assert built == ["v4.31.0"]
        assert bg == []  # no background fallback when the sync build succeeds

    def test_missing_toolchain_falls_back_without_network(
        self, mock_runtime, patch_hook, monkeypatch
    ):
        built = []
        monkeypatch.setattr(
            builder_mod,
            "build_lean_toolchain_image",
            lambda runtime, version: built.append(version),
            raising=False,
        )
        bg = []
        monkeypatch.setattr(
            image_management,
            "_background_build_lean_toolchain",
            lambda version: bg.append(version),
        )
        # plain lean image exists so no synchronous base build happens.
        mock_runtime._images.add("lean")

        hook, image_name = image_management.detect_and_build_image(
            mock_runtime, "/ref", _Target("branch", "main"), restricted_network=False
        )

        assert image_name == "lean"
        assert built == []  # no synchronous toolchain build off-network
        assert bg == ["v4.31.0"]  # background build queued for next time

    def test_sync_build_failure_under_network_fails_fast(
        self, mock_runtime, patch_hook, monkeypatch
    ):
        import click

        def _boom(runtime, version):
            raise RuntimeError("network blip")

        monkeypatch.setattr(builder_mod, "build_lean_toolchain_image", _boom, raising=False)
        bg = []
        monkeypatch.setattr(
            image_management,
            "_background_build_lean_toolchain",
            lambda version: bg.append(version),
        )
        mock_runtime._images.add("lean")

        # Falling back to plain lean here would reintroduce the blocked-download
        # hang, so a build failure under the allowlist aborts with a clear error.
        with pytest.raises(click.ClickException):
            image_management.detect_and_build_image(
                mock_runtime, "/ref", _Target("branch", "main"), restricted_network=True
            )
        assert bg == []  # no background build scheduled on hard failure

    def test_cached_toolchain_image_used_directly(self, mock_runtime, patch_hook, monkeypatch):
        built = []
        monkeypatch.setattr(
            builder_mod,
            "build_lean_toolchain_image",
            lambda runtime, version: built.append(version),
            raising=False,
        )
        mock_runtime._images.add("lean-v4.31.0")

        hook, image_name = image_management.detect_and_build_image(
            mock_runtime, "/ref", _Target("branch", "main"), restricted_network=True
        )

        assert image_name == "lean-v4.31.0"
        assert built == []  # already cached, no build
