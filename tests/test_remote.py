"""Tests for remote SSH host support."""

import tarfile
from unittest.mock import MagicMock, patch

import pytest

from bubble.remote import RemoteHost, _create_bundle, _find_package_dirs, remote_open


class TestRemoteHostParse:
    def test_bare_host(self):
        h = RemoteHost.parse("myserver")
        assert h.hostname == "myserver"
        assert h.user is None
        assert h.port == 22

    def test_user_at_host(self):
        h = RemoteHost.parse("kim@myserver")
        assert h.hostname == "myserver"
        assert h.user == "kim"
        assert h.port == 22

    def test_host_with_port(self):
        h = RemoteHost.parse("myserver:2222")
        assert h.hostname == "myserver"
        assert h.user is None
        assert h.port == 2222

    def test_user_at_host_with_port(self):
        h = RemoteHost.parse("kim@myserver:2222")
        assert h.hostname == "myserver"
        assert h.user == "kim"
        assert h.port == 2222

    def test_default_port(self):
        h = RemoteHost.parse("kim@myserver:22")
        assert h.port == 22

    def test_empty_hostname_raises(self):
        with pytest.raises(ValueError, match="Empty hostname"):
            RemoteHost.parse("")

    def test_empty_user_raises(self):
        with pytest.raises(ValueError, match="Empty user"):
            RemoteHost.parse("@myserver")

    def test_invalid_port_raises(self):
        with pytest.raises(ValueError, match="Invalid port"):
            RemoteHost.parse("myserver:notaport")

    def test_port_out_of_range_raises(self):
        with pytest.raises(ValueError, match="Port out of range"):
            RemoteHost.parse("myserver:99999")

    def test_port_zero_raises(self):
        with pytest.raises(ValueError, match="Port out of range"):
            RemoteHost.parse("myserver:0")


class TestRemoteHostValidation:
    """Test hostname and user validation to prevent injection attacks."""

    def test_hostname_with_dash_prefix_rejected(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            RemoteHost.parse("-oProxyCommand=evil")

    def test_user_with_dash_prefix_rejected(self):
        with pytest.raises(ValueError, match="Invalid user"):
            RemoteHost.parse("-evil@server")

    def test_hostname_with_spaces_rejected(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            RemoteHost.parse("kim@my server")

    def test_hostname_with_semicolons_rejected(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            RemoteHost.parse("host;rm -rf /")

    def test_hostname_with_dollar_rejected(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            RemoteHost.parse("$(whoami)")

    def test_hostname_with_backticks_rejected(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            RemoteHost.parse("`whoami`")

    def test_user_with_shell_metachar_rejected(self):
        with pytest.raises(ValueError, match="Invalid user"):
            RemoteHost.parse("us;er@server")

    def test_fqdn_hostname_accepted(self):
        h = RemoteHost.parse("build.example.com")
        assert h.hostname == "build.example.com"

    def test_dotted_user_accepted(self):
        h = RemoteHost.parse("kim.morrison@server")
        assert h.user == "kim.morrison"

    def test_hyphenated_hostname_accepted(self):
        h = RemoteHost.parse("build-server-01")
        assert h.hostname == "build-server-01"

    def test_underscore_user_accepted(self):
        h = RemoteHost.parse("kim_m@server")
        assert h.user == "kim_m"


class TestRemoteHostProperties:
    def test_ssh_destination_with_user(self):
        h = RemoteHost(hostname="server", user="kim")
        assert h.ssh_destination == "kim@server"

    def test_ssh_destination_without_user(self):
        h = RemoteHost(hostname="server")
        assert h.ssh_destination == "server"

    def test_ssh_cmd_default_port(self):
        h = RemoteHost(hostname="server", user="kim")
        cmd = h.ssh_cmd(["ls", "-la"])
        assert cmd == ["ssh", "kim@server", "ls", "-la"]

    def test_ssh_cmd_custom_port(self):
        h = RemoteHost(hostname="server", user="kim", port=2222)
        cmd = h.ssh_cmd(["ls", "-la"])
        assert cmd == ["ssh", "-p", "2222", "kim@server", "ls", "-la"]

    def test_scp_cmd_default_port(self):
        h = RemoteHost(hostname="server", user="kim")
        cmd = h.scp_cmd("/tmp/file", "/remote/path")
        assert cmd == ["scp", "-q", "/tmp/file", "kim@server:/remote/path"]

    def test_scp_cmd_custom_port(self):
        h = RemoteHost(hostname="server", user="kim", port=2222)
        cmd = h.scp_cmd("/tmp/file", "/remote/path")
        assert cmd == ["scp", "-q", "-P", "2222", "/tmp/file", "kim@server:/remote/path"]

    def test_spec_string_basic(self):
        h = RemoteHost(hostname="server", user="kim")
        assert h.spec_string() == "kim@server"

    def test_spec_string_with_port(self):
        h = RemoteHost(hostname="server", user="kim", port=2222)
        assert h.spec_string() == "kim@server:2222"

    def test_spec_string_no_user(self):
        h = RemoteHost(hostname="server")
        assert h.spec_string() == "server"

    def test_spec_string_no_user_with_port(self):
        h = RemoteHost(hostname="server", port=2222)
        assert h.spec_string() == "server:2222"

    def test_spec_string_default_port_omitted(self):
        h = RemoteHost(hostname="server", user="kim", port=22)
        assert h.spec_string() == "kim@server"


class TestSpecRoundtrip:
    """Parsing a spec_string should produce the same RemoteHost."""

    @pytest.mark.parametrize(
        "spec",
        [
            "server",
            "kim@server",
            "server:2222",
            "kim@server:2222",
        ],
    )
    def test_roundtrip(self, spec):
        h = RemoteHost.parse(spec)
        assert RemoteHost.parse(h.spec_string()) == h


class TestFindPackageDirs:
    def test_finds_bubble(self):
        dirs = _find_package_dirs()
        assert "bubble" in dirs
        assert dirs["bubble"].is_dir()

    def test_finds_click(self):
        dirs = _find_package_dirs()
        assert "click" in dirs
        assert dirs["click"].is_dir()

    def test_finds_tomli_w(self):
        dirs = _find_package_dirs()
        assert "tomli_w" in dirs
        assert dirs["tomli_w"].is_dir()


class TestRemoteOpenFlagForwarding:
    """Test that CLI flags are forwarded to the remote bubble open command."""

    def _run_remote_open(self, **kwargs):
        """Helper: call remote_open with mocks, return the command string."""
        host = RemoteHost(hostname="test.example.com", user="root")
        defaults = {"host": host, "target": "owner/repo"}
        defaults.update(kwargs)
        host = defaults.pop("host")
        target = defaults.pop("target")

        with (
            patch("bubble.remote.ensure_remote_bubble"),
            patch("bubble.remote._find_remote_python", return_value="python3"),
            patch("subprocess.Popen") as mock_popen,
        ):
            proc = MagicMock()
            proc.stdout.__iter__ = MagicMock(
                return_value=iter(['{"name": "test", "status": "ok"}\n'])
            )
            proc.stderr = MagicMock()
            proc.stderr.read = MagicMock(return_value="")
            proc.wait = MagicMock(return_value=0)
            proc.returncode = 0
            mock_popen.return_value = proc

            try:
                remote_open(host, target, **defaults)
            except Exception:
                pass

            call_args = mock_popen.call_args
            cmd = call_args[0][0] if call_args[0] else call_args[1].get("args", [])
            return " ".join(cmd) if isinstance(cmd, list) else cmd

    def test_new_branch_forwarded(self):
        cmd_str = self._run_remote_open(new_branch="my-feature")
        assert "-b" in cmd_str
        assert "my-feature" in cmd_str

    def test_base_ref_forwarded(self):
        cmd_str = self._run_remote_open(new_branch="feat", base_ref="develop")
        assert "-b" in cmd_str
        assert "feat" in cmd_str
        assert "--base" in cmd_str
        assert "develop" in cmd_str

    def test_no_branch_flags_by_default(self):
        cmd_str = self._run_remote_open()
        assert " -b " not in cmd_str
        assert "--base" not in cmd_str

    def test_base_ref_alone_forwarded(self):
        """--base without -b is still forwarded to the remote command."""
        cmd_str = self._run_remote_open(base_ref="main")
        assert "--base" in cmd_str
        assert "main" in cmd_str


class TestCreateBundle:
    def test_creates_tarball(self):
        bundle = _create_bundle()
        try:
            assert bundle.exists()
            assert bundle.suffix == ".gz"
            assert bundle.stat().st_size > 0

            # Verify it's a valid tarball containing expected packages
            with tarfile.open(bundle, "r:gz") as tar:
                names = tar.getnames()
                assert any(n.startswith("bubble/") for n in names)
                assert any(n.startswith("click/") for n in names)
                # No __pycache__ should be included
                assert not any("__pycache__" in n for n in names)
                assert not any(n.endswith(".pyc") for n in names)
        finally:
            bundle.unlink(missing_ok=True)
