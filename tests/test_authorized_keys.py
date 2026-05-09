"""Tests for authorized_keys collection logic."""

import click
import pytest

from bubble.container_helpers import collect_authorized_keys


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Provide a temporary HOME with an empty .ssh dir."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    # Also clear the env var so tests don't pick up a real one
    monkeypatch.delenv("BUBBLE_AUTHORIZED_KEYS", raising=False)
    return tmp_path


def _write_key(ssh_dir, name, content="ssh-ed25519 AAAA test"):
    path = ssh_dir / name
    path.write_text(content + "\n")
    return path


class TestDefaults:
    def test_no_keys_returns_empty(self, fake_home):
        assert collect_authorized_keys() == []
        assert collect_authorized_keys({}) == []

    def test_only_ed25519(self, fake_home):
        _write_key(fake_home / ".ssh", "id_ed25519.pub", "ssh-ed25519 AAAA me")
        keys = collect_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA me"]

    def test_ed25519_preferred_over_rsa(self, fake_home):
        _write_key(fake_home / ".ssh", "id_ed25519.pub", "ssh-ed25519 AAAA me")
        _write_key(fake_home / ".ssh", "id_rsa.pub", "ssh-rsa AAAA old")
        keys = collect_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA me"]

    def test_falls_back_to_rsa_when_no_ed25519(self, fake_home):
        _write_key(fake_home / ".ssh", "id_rsa.pub", "ssh-rsa AAAA old")
        keys = collect_authorized_keys()
        assert keys == ["ssh-rsa AAAA old"]

    def test_falls_back_to_ecdsa(self, fake_home):
        _write_key(fake_home / ".ssh", "id_ecdsa.pub", "ecdsa-sha2 AAAA card")
        keys = collect_authorized_keys()
        assert keys == ["ecdsa-sha2 AAAA card"]

    def test_includes_both_rsa_and_ecdsa_when_no_ed25519(self, fake_home):
        _write_key(fake_home / ".ssh", "id_rsa.pub", "ssh-rsa AAAA old")
        _write_key(fake_home / ".ssh", "id_ecdsa.pub", "ecdsa-sha2 AAAA card")
        keys = collect_authorized_keys()
        assert "ssh-rsa AAAA old" in keys
        assert "ecdsa-sha2 AAAA card" in keys
        assert len(keys) == 2

    def test_empty_pub_file_skipped(self, fake_home):
        (fake_home / ".ssh" / "id_ed25519.pub").write_text("")
        assert collect_authorized_keys() == []


class TestConfigOverride:
    def test_string_path(self, fake_home):
        path = _write_key(fake_home / ".ssh", "yubikey.pub", "ssh-ed25519 AAAA yk")
        # Also drop a default key to confirm it's ignored
        _write_key(fake_home / ".ssh", "id_ed25519.pub", "ssh-ed25519 AAAA me")
        cfg = {"ssh": {"authorized_keys": str(path)}}
        keys = collect_authorized_keys(cfg)
        assert keys == ["ssh-ed25519 AAAA yk"]

    def test_list_paths(self, fake_home):
        p1 = _write_key(fake_home / ".ssh", "yubikey.pub", "ssh-ed25519 AAAA yk")
        p2 = _write_key(fake_home / ".ssh", "laptop.pub", "ssh-ed25519 AAAA lap")
        _write_key(fake_home / ".ssh", "id_ed25519.pub", "ssh-ed25519 AAAA me")
        cfg = {"ssh": {"authorized_keys": [str(p1), str(p2)]}}
        keys = collect_authorized_keys(cfg)
        assert keys == ["ssh-ed25519 AAAA yk", "ssh-ed25519 AAAA lap"]

    def test_tilde_expansion(self, fake_home):
        _write_key(fake_home / ".ssh", "yubikey.pub", "ssh-ed25519 AAAA yk")
        cfg = {"ssh": {"authorized_keys": "~/.ssh/yubikey.pub"}}
        keys = collect_authorized_keys(cfg)
        assert keys == ["ssh-ed25519 AAAA yk"]

    def test_missing_explicit_path_raises(self, fake_home):
        cfg = {"ssh": {"authorized_keys": str(fake_home / ".ssh" / "nope.pub")}}
        with pytest.raises(click.ClickException, match="not found"):
            collect_authorized_keys(cfg)

    def test_empty_list_returns_no_keys(self, fake_home):
        _write_key(fake_home / ".ssh", "id_ed25519.pub", "ssh-ed25519 AAAA me")
        cfg = {"ssh": {"authorized_keys": []}}
        # Explicit empty list overrides defaults
        assert collect_authorized_keys(cfg) == []

    def test_empty_string_returns_no_keys(self, fake_home):
        _write_key(fake_home / ".ssh", "id_ed25519.pub", "ssh-ed25519 AAAA me")
        cfg = {"ssh": {"authorized_keys": ""}}
        assert collect_authorized_keys(cfg) == []

    def test_invalid_type_raises(self, fake_home):
        cfg = {"ssh": {"authorized_keys": 42}}
        with pytest.raises(click.ClickException, match="must be a string or list"):
            collect_authorized_keys(cfg)


class TestEnvVar:
    def test_env_var_overrides_config(self, fake_home, monkeypatch):
        path = _write_key(fake_home / ".ssh", "env.pub", "ssh-ed25519 AAAA env")
        cfg_path = _write_key(fake_home / ".ssh", "cfg.pub", "ssh-ed25519 AAAA cfg")
        monkeypatch.setenv("BUBBLE_AUTHORIZED_KEYS", str(path))
        cfg = {"ssh": {"authorized_keys": str(cfg_path)}}
        keys = collect_authorized_keys(cfg)
        assert keys == ["ssh-ed25519 AAAA env"]

    def test_env_var_colon_separated(self, fake_home, monkeypatch):
        p1 = _write_key(fake_home / ".ssh", "a.pub", "ssh-ed25519 AAAA a")
        p2 = _write_key(fake_home / ".ssh", "b.pub", "ssh-ed25519 AAAA b")
        monkeypatch.setenv("BUBBLE_AUTHORIZED_KEYS", f"{p1}:{p2}")
        keys = collect_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA a", "ssh-ed25519 AAAA b"]

    def test_env_var_missing_path_raises(self, fake_home, monkeypatch):
        monkeypatch.setenv("BUBBLE_AUTHORIZED_KEYS", str(fake_home / ".ssh" / "nope.pub"))
        with pytest.raises(click.ClickException, match="not found"):
            collect_authorized_keys()
