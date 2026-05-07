"""Tests for VSCode SSH config generation and bubble name validation."""

import subprocess

import pytest

from bubble.remote import RemoteHost
from bubble.vscode import _BUBBLE_NAME_RE, add_ssh_config, open_vscode, remove_ssh_config


class TestBubbleNameValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "mathlib4-pr-12345",
            "lean4-main-20260213",
            "batteries-branch-fix-grind",
            "a",
            "test",
        ],
    )
    def test_valid_names_accepted(self, name):
        assert _BUBBLE_NAME_RE.match(name)

    @pytest.mark.parametrize(
        "name",
        [
            "UPPER",
            "123-starts-with-digit",
            "has spaces",
            "has;semicolons",
            "has$(cmd)",
            "-starts-with-dash",
        ],
    )
    def test_invalid_names_rejected(self, name):
        assert not _BUBBLE_NAME_RE.match(name)

    def test_empty_string_rejected(self):
        assert not _BUBBLE_NAME_RE.match("")


class TestAddSshConfig:
    def test_writes_proxy_command(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        add_ssh_config("test-bubble")
        content = ssh_file.read_text()
        assert "Host bubble-test-bubble" in content
        assert "ProxyCommand incus exec test-bubble" in content
        assert "nc localhost 22" in content

    def test_rejects_invalid_name(self, tmp_ssh_dir):
        with pytest.raises(ValueError, match="Invalid bubble name"):
            add_ssh_config("evil; rm -rf /")


class TestRemoveSshConfig:
    def test_removes_correct_entry(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        # Write two entries
        add_ssh_config("keep-this")
        add_ssh_config("remove-this")

        content_before = ssh_file.read_text()
        assert "keep-this" in content_before
        assert "remove-this" in content_before

        remove_ssh_config("remove-this")
        content_after = ssh_file.read_text()
        assert "keep-this" in content_after
        assert "remove-this" not in content_after

    def test_noop_for_nonexistent(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        add_ssh_config("existing")
        content_before = ssh_file.read_text()
        remove_ssh_config("nonexistent")
        content_after = ssh_file.read_text()
        assert content_before == content_after


class TestAtomicSshConfig:
    """SSH config writes survive process death and don't clobber dotfiles symlinks."""

    def test_failed_write_leaves_original_intact(self, tmp_ssh_dir, monkeypatch):
        """If os.replace raises mid-write, the original ~/.ssh/config is unchanged."""
        import os as real_os

        from bubble import vscode

        add_ssh_config("first")
        original = (tmp_ssh_dir / "bubble").read_text()

        replace_calls = {"n": 0}
        real_replace = real_os.replace

        def flaky_replace(src, dst):
            # Let the SSH_MAIN_CONFIG include-directive write succeed; fail on
            # the bubble-config write itself.
            if str(dst).endswith("/bubble"):
                replace_calls["n"] += 1
                raise OSError("simulated kill mid-replace")
            return real_replace(src, dst)

        monkeypatch.setattr(vscode.os, "replace", flaky_replace)

        with pytest.raises(OSError, match="simulated"):
            add_ssh_config("second")

        # Original content preserved
        assert (tmp_ssh_dir / "bubble").read_text() == original
        assert replace_calls["n"] == 1
        # No leftover temp files in the dir
        leftovers = [p.name for p in tmp_ssh_dir.iterdir() if p.name.startswith("bubble.")]
        assert leftovers == []

    def test_main_config_symlink_is_preserved(self, tmp_ssh_dir, tmp_path):
        """Writing through a symlinked ~/.ssh/config updates the target, not the link."""
        from bubble import vscode

        # Simulate a dotfiles-style setup: ~/.ssh/config -> dotfiles/ssh_config
        dotfiles = tmp_path / "dotfiles"
        dotfiles.mkdir()
        real_target = dotfiles / "ssh_config"
        real_target.write_text("# user dotfiles\n")
        vscode.SSH_MAIN_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        vscode.SSH_MAIN_CONFIG.symlink_to(real_target)

        add_ssh_config("symlink-bubble")

        # Symlink itself is preserved
        assert vscode.SSH_MAIN_CONFIG.is_symlink()
        assert vscode.SSH_MAIN_CONFIG.resolve() == real_target.resolve()
        # Original content preserved + Include line prepended
        content = real_target.read_text()
        assert "# user dotfiles" in content
        assert "Include " in content

    def test_concurrent_adds_do_not_lose_entries(self, tmp_ssh_dir):
        """Two processes calling add_ssh_config concurrently both win."""
        import multiprocessing as mp

        # Use spawn so child re-imports modules cleanly
        ctx = mp.get_context("fork")
        procs = [ctx.Process(target=add_ssh_config, args=(f"concurrent-{i}",)) for i in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)
            assert p.exitcode == 0

        content = (tmp_ssh_dir / "bubble").read_text()
        for i in range(8):
            assert f"Host bubble-concurrent-{i}" in content


class TestRemoteProxyCommand:
    def test_chained_proxy_with_default_port(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        host = RemoteHost(hostname="build-server", user="kim")
        add_ssh_config("test-remote", remote_host=host)
        content = ssh_file.read_text()
        assert "Host bubble-test-remote" in content
        assert "ProxyCommand ssh kim@build-server" in content
        assert "nc localhost 22" in content
        # Remote command should be single-quoted to survive shell hops
        assert "'incus exec test-remote" in content
        # Should NOT have -p flag for default port
        assert "-p 22" not in content

    def test_chained_proxy_with_custom_port(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        host = RemoteHost(hostname="build-server", user="kim", port=2222)
        add_ssh_config("test-remote", remote_host=host)
        content = ssh_file.read_text()
        assert "ProxyCommand ssh -p 2222 kim@build-server" in content
        assert "'incus exec test-remote" in content

    def test_chained_proxy_without_user(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        host = RemoteHost(hostname="build-server")
        add_ssh_config("test-remote", remote_host=host)
        content = ssh_file.read_text()
        assert "ProxyCommand ssh build-server" in content
        assert "'incus exec test-remote" in content

    def test_local_proxy_unchanged(self, tmp_ssh_dir):
        """Without remote_host, ProxyCommand should use incus exec directly."""
        ssh_file = tmp_ssh_dir / "bubble"
        add_ssh_config("test-local")
        content = ssh_file.read_text()
        assert "ProxyCommand incus exec test-local" in content
        assert "ssh " not in content.split("ProxyCommand")[1].split("\n")[0]  # no ssh in proxy


class TestOpenVscodeWorkspace:
    def test_folder_uri_without_workspace(self, monkeypatch):
        """Without workspace file, uses --folder-uri."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_vscode("test-bubble", "/home/user/lean4")
        assert len(calls) == 1
        assert "--folder-uri" in calls[0]
        assert "--file-uri" not in calls[0]

    def test_file_uri_with_workspace(self, monkeypatch):
        """With workspace file, uses --file-uri."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_vscode(
            "test-bubble",
            "/home/user/lean4",
            workspace_file="/home/user/lean4/lean.code-workspace",
        )
        assert len(calls) == 1
        assert "--file-uri" in calls[0]
        assert "--folder-uri" not in calls[0]

    def test_workspace_uri_format(self, monkeypatch):
        """Workspace file URI has correct format."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_vscode(
            "test-bubble",
            "/home/user/lean4",
            workspace_file="/home/user/lean4/lean.code-workspace",
        )
        uri = calls[0][-1]
        assert uri == (
            "vscode-remote://ssh-remote+bubble-test-bubble/home/user/lean4/lean.code-workspace"
        )
