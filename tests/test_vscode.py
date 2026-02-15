"""Tests for VSCode SSH config generation and bubble name validation."""

import pytest

from bubble.remote import RemoteHost
from bubble.vscode import _BUBBLE_NAME_RE, add_ssh_config, remove_ssh_config


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


class TestRemoteProxyCommand:
    def test_chained_proxy_with_default_port(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        host = RemoteHost(hostname="build-server", user="kim")
        add_ssh_config("test-remote", remote_host=host)
        content = ssh_file.read_text()
        assert "Host bubble-test-remote" in content
        assert "ProxyCommand ssh kim@build-server incus exec test-remote" in content
        assert "nc localhost 22" in content
        # Should NOT have -p flag for default port
        assert "-p 22" not in content

    def test_chained_proxy_with_custom_port(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        host = RemoteHost(hostname="build-server", user="kim", port=2222)
        add_ssh_config("test-remote", remote_host=host)
        content = ssh_file.read_text()
        assert "ProxyCommand ssh -p 2222 kim@build-server incus exec test-remote" in content

    def test_chained_proxy_without_user(self, tmp_ssh_dir):
        ssh_file = tmp_ssh_dir / "bubble"
        host = RemoteHost(hostname="build-server")
        add_ssh_config("test-remote", remote_host=host)
        content = ssh_file.read_text()
        assert "ProxyCommand ssh build-server incus exec test-remote" in content

    def test_local_proxy_unchanged(self, tmp_ssh_dir):
        """Without remote_host, ProxyCommand should use incus exec directly."""
        ssh_file = tmp_ssh_dir / "bubble"
        add_ssh_config("test-local")
        content = ssh_file.read_text()
        assert "ProxyCommand incus exec test-local" in content
        assert "ssh " not in content.split("ProxyCommand")[1].split("\n")[0]  # no ssh in proxy
