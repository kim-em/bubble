"""Tests for Hetzner Cloud support."""

import json
from pathlib import Path

import pytest

try:
    import hcloud  # noqa: F401
    HAS_HCLOUD = True
except ImportError:
    HAS_HCLOUD = False

from bubble.cloud import (
    _clear_state,
    _ensure_ssh_key,
    _get_cloud_init,
    _load_state,
    _save_state,
    _ssh_cmd_base,
)
from bubble.remote import RemoteHost


class TestCloudState:
    """Test cloud state file I/O."""

    def test_save_and_load(self, tmp_data_dir):
        state = {
            "provider": "hetzner",
            "server_id": 12345,
            "server_name": "bubble-cloud",
            "ipv4": "1.2.3.4",
            "server_type": "ccx43",
            "location": "fsn1",
            "ssh_key_id": 67890,
        }
        _save_state(state)
        loaded = _load_state()
        assert loaded == state

    def test_load_empty(self, tmp_data_dir):
        assert _load_state() is None

    def test_clear(self, tmp_data_dir):
        _save_state({"test": True})
        assert _load_state() is not None
        _clear_state()
        assert _load_state() is None

    def test_clear_nonexistent(self, tmp_data_dir):
        # Should not raise
        _clear_state()

    def test_state_file_is_json(self, tmp_data_dir):
        _save_state({"provider": "hetzner", "server_id": 42})
        content = (tmp_data_dir / "cloud.json").read_text()
        data = json.loads(content)
        assert data["server_id"] == 42


class TestCloudInit:
    """Test cloud-init script generation."""

    def test_default_idle_timeout(self):
        script = _get_cloud_init({})
        assert "IDLE_TIMEOUT=900" in script

    def test_custom_idle_timeout(self):
        script = _get_cloud_init({"cloud": {"idle_timeout": 1800}})
        assert "IDLE_TIMEOUT=1800" in script

    def test_installs_incus(self):
        script = _get_cloud_init({})
        assert "apt-get install -y incus" in script
        assert "incus admin init --auto" in script

    def test_installs_idle_timer(self):
        script = _get_cloud_init({})
        assert "bubble-idle.timer" in script
        assert "bubble-idle-check" in script
        assert "systemctl enable --now bubble-idle.timer" in script

    def test_readiness_marker(self):
        script = _get_cloud_init({})
        assert "/var/run/bubble-cloud-ready" in script

    def test_idle_checks_ssh_connections(self):
        script = _get_cloud_init({})
        assert "dport = :22" in script

    def test_idle_checks_cpu_load(self):
        script = _get_cloud_init({})
        assert "/proc/loadavg" in script
        assert "nproc" in script

    def test_idle_does_not_check_containers(self):
        """Idle check should NOT prevent shutdown based on container state.
        Containers survive server restart."""
        script = _get_cloud_init({})
        # The idle check script should not reference incus list
        idle_script_start = script.index("bubble-idle-check <<")
        idle_script_end = script.index("IDLESCRIPT")
        idle_section = script[idle_script_start:idle_script_end]
        assert "incus list" not in idle_section
        assert "incus" not in idle_section


class TestSSHKey:
    """Test SSH key management."""

    def test_ensure_creates_key(self, tmp_data_dir):
        priv_path, pub_content = _ensure_ssh_key()
        assert Path(priv_path).exists()
        assert Path(priv_path).with_suffix(".pub").exists()
        assert "ssh-ed25519" in pub_content
        assert "bubble-cloud" in pub_content
        # Key should be mode 0600
        assert oct(Path(priv_path).stat().st_mode & 0o777) == "0o600"

    def test_ensure_idempotent(self, tmp_data_dir):
        priv1, pub1 = _ensure_ssh_key()
        priv2, pub2 = _ensure_ssh_key()
        assert priv1 == priv2
        assert pub1 == pub2

    def test_ssh_cmd_base(self, tmp_data_dir):
        cmd = _ssh_cmd_base()
        assert cmd[0] == "ssh"
        assert "-i" in cmd
        # The key path should point into the tmp data dir
        i_idx = cmd.index("-i")
        assert "cloud_key" in cmd[i_idx + 1]
        joined = " ".join(cmd)
        assert "IdentitiesOnly=yes" in joined
        assert "known_hosts" in joined


class TestRemoteHostConstruction:
    """Test that cloud module produces correct RemoteHost instances."""

    def test_cloud_remote_host_is_root(self):
        host = RemoteHost(hostname="1.2.3.4", user="root", port=22)
        assert host.ssh_destination == "root@1.2.3.4"
        assert host.user == "root"

    def test_spec_string(self):
        host = RemoteHost(hostname="1.2.3.4", user="root", port=22)
        assert host.spec_string() == "root@1.2.3.4"


@pytest.mark.skipif(not HAS_HCLOUD, reason="hcloud not installed")
class TestProvisionValidation:
    """Test provision_server input validation (no actual API calls)."""

    def test_default_server_type(self, tmp_data_dir):
        """When no server_type is configured, cx43 is used as default."""
        from unittest.mock import MagicMock, patch

        from bubble.cloud import provision_server

        config = {"cloud": {"server_type": "", "location": "fsn1"}}
        with patch("bubble.cloud._get_client") as mock_client, \
             patch("bubble.cloud._ensure_ssh_key", return_value=("/tmp/key", "ssh-ed25519 AAAA")):
            client = MagicMock()
            mock_client.return_value = client
            # Make SSH key creation succeed
            ssh_key = MagicMock()
            ssh_key.data_model.id = 1
            client.ssh_keys.create.return_value = ssh_key
            # Make server creation fail so we don't need full setup
            from hcloud._exceptions import APIException
            client.servers.create.side_effect = APIException(
                code="test", message="test error", details={}
            )
            with pytest.raises(Exception):
                provision_server(config)
            # Verify cx43 was used as the server type
            call_kwargs = client.servers.create.call_args
            assert call_kwargs.kwargs["server_type"].name == "cx43"

    def test_existing_server_errors(self, tmp_data_dir):
        from click import ClickException

        from bubble.cloud import provision_server

        _save_state({"server_name": "test", "ipv4": "1.2.3.4"})
        config = {"cloud": {"server_type": "cx33"}}
        with pytest.raises(ClickException, match="already exists"):
            provision_server(config)

    def test_no_state_for_cloud_remote_host(self, tmp_data_dir):
        from click import ClickException

        from bubble.cloud import get_cloud_remote_host

        with pytest.raises(ClickException, match="No cloud server provisioned"):
            get_cloud_remote_host({})
