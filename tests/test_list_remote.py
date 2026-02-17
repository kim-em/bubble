"""Tests for remote-aware bubble list."""

import json

import pytest

from bubble.cli import _is_cloud_host, _parse_iso, _remote_entries_from_registry
from bubble.cloud import _save_state
from bubble.lifecycle import register_bubble
from bubble.remote import RemoteHost, apply_cloud_ssh_options


class TestParseIso:
    """Test ISO datetime parsing helper."""

    def test_valid_iso(self):
        dt = _parse_iso("2025-02-17T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2025

    def test_none_input(self):
        assert _parse_iso(None) is None

    def test_empty_string(self):
        assert _parse_iso("") is None

    def test_invalid_string(self):
        assert _parse_iso("not-a-date") is None

    def test_naive_datetime_gets_utc(self):
        dt = _parse_iso("2025-02-17T10:30:00")
        assert dt is not None
        assert dt.tzinfo is not None


class TestIsCloudHost:
    """Test cloud host detection."""

    def test_matches_cloud_ip(self, tmp_data_dir):
        _save_state({"ipv4": "1.2.3.4", "server_id": 1})
        assert _is_cloud_host("1.2.3.4") is True

    def test_no_match(self, tmp_data_dir):
        _save_state({"ipv4": "1.2.3.4", "server_id": 1})
        assert _is_cloud_host("5.6.7.8") is False

    def test_no_cloud_state(self, tmp_data_dir):
        assert _is_cloud_host("1.2.3.4") is False


class TestApplyCloudSshOptions:
    """Test apply_cloud_ssh_options."""

    def test_adds_options_for_cloud_host(self, tmp_data_dir):
        _save_state({"ipv4": "1.2.3.4", "server_id": 1})
        host = RemoteHost(hostname="1.2.3.4", user="root")
        assert host.ssh_options is None
        apply_cloud_ssh_options(host)
        assert host.ssh_options is not None
        assert "-i" in host.ssh_options
        assert "IdentitiesOnly=yes" in host.ssh_options

    def test_no_options_for_non_cloud(self, tmp_data_dir):
        _save_state({"ipv4": "1.2.3.4", "server_id": 1})
        host = RemoteHost(hostname="5.6.7.8", user="root")
        apply_cloud_ssh_options(host)
        assert host.ssh_options is None

    def test_no_options_without_state(self, tmp_data_dir):
        host = RemoteHost(hostname="1.2.3.4", user="root")
        apply_cloud_ssh_options(host)
        assert host.ssh_options is None


class TestRemoteEntriesFromRegistry:
    """Test _remote_entries_from_registry."""

    def test_empty_registry(self, tmp_data_dir):
        assert _remote_entries_from_registry() == []

    def test_local_bubbles_skipped(self, tmp_data_dir):
        register_bubble("local-bubble", "owner/repo")
        assert _remote_entries_from_registry() == []

    def test_remote_bubbles_included(self, tmp_data_dir):
        register_bubble("remote-bubble", "owner/repo", remote_host="root@1.2.3.4")
        entries = _remote_entries_from_registry()
        assert len(entries) == 1
        assert entries[0]["name"] == "remote-bubble"
        assert entries[0]["state"] == "unknown"
        assert entries[0]["remote_host_spec"] == "root@1.2.3.4"

    def test_cloud_location_detected(self, tmp_data_dir):
        _save_state({"ipv4": "1.2.3.4", "server_id": 1})
        register_bubble("cloud-bubble", "owner/repo", remote_host="root@1.2.3.4")
        entries = _remote_entries_from_registry()
        assert len(entries) == 1
        assert entries[0]["location"] == "cloud"

    def test_ssh_location_shows_spec(self, tmp_data_dir):
        register_bubble("ssh-bubble", "owner/repo", remote_host="user@myserver")
        entries = _remote_entries_from_registry()
        assert len(entries) == 1
        assert entries[0]["location"] == "user@myserver"

    def test_multiple_hosts(self, tmp_data_dir):
        register_bubble("b1", "owner/repo1", remote_host="root@1.2.3.4")
        register_bubble("b2", "owner/repo2", remote_host="user@other")
        entries = _remote_entries_from_registry()
        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert names == {"b1", "b2"}

    def test_created_at_parsed(self, tmp_data_dir):
        register_bubble("b1", "owner/repo", remote_host="root@1.2.3.4")
        entries = _remote_entries_from_registry()
        assert entries[0]["created_at"] is not None
