"""Tests for the bubble status command."""

import json

from click.testing import CliRunner

from bubble.cli import main
from bubble.runtime.base import ContainerInfo


def _patch_runtime(monkeypatch, mock_runtime):
    """Patch get_runtime in the module where status_cmd imports it."""
    # MockRuntime.list_containers doesn't accept fast=, so patch it
    orig_list = mock_runtime.list_containers
    mock_runtime.list_containers = lambda fast=True: orig_list()
    monkeypatch.setattr(
        "bubble.commands.status_cmd.get_runtime",
        lambda config, ensure_ready=True: mock_runtime,
    )


def test_status_no_bubbles(tmp_data_dir, mock_runtime, monkeypatch):
    """Status with no bubbles shows 'none'."""
    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "none" in result.output


def test_status_with_running_bubbles(tmp_data_dir, mock_runtime, monkeypatch):
    """Status shows running bubble count."""
    mock_runtime._containers = {
        "mathlib4-pr-123": ContainerInfo(name="mathlib4-pr-123", state="running"),
        "lean4-main": ContainerInfo(name="lean4-main", state="running"),
        "test-pr-456": ContainerInfo(name="test-pr-456", state="frozen"),
    }
    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "2 running" in result.output
    assert "1 paused" in result.output


def test_status_shows_tools(tmp_data_dir, mock_runtime, monkeypatch):
    """Status shows enabled tools."""
    _patch_runtime(monkeypatch, mock_runtime)
    monkeypatch.setattr("bubble.tools.resolve_tools", lambda config: ["claude", "elan", "vscode"])

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "claude, elan, vscode" in result.output


def test_status_shows_remote(tmp_data_dir, mock_runtime, monkeypatch):
    """Status shows default remote host when configured."""
    # Write config with remote.default_host
    import tomli_w

    from bubble.config import CONFIG_FILE

    CONFIG_FILE.write_bytes(tomli_w.dumps({"remote": {"default_host": "myserver"}}).encode())

    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "myserver" in result.output
    assert "Remote:" in result.output


def test_status_hides_remote_when_not_configured(tmp_data_dir, mock_runtime, monkeypatch):
    """Status omits Remote line when no default host."""
    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Remote:" not in result.output


def test_status_shows_cloud(tmp_data_dir, mock_runtime, monkeypatch):
    """Status shows cloud server info from local state file."""
    cloud_state = {
        "server_name": "bubble-cloud",
        "server_type": "cx43",
        "location": "fsn1",
        "ipv4": "1.2.3.4",
        "server_id": 12345,
    }
    from bubble.config import CLOUD_STATE_FILE

    CLOUD_STATE_FILE.write_text(json.dumps(cloud_state))

    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Cloud:" in result.output
    assert "cx43" in result.output
    assert "fsn1" in result.output


def test_status_hides_cloud_when_not_provisioned(tmp_data_dir, mock_runtime, monkeypatch):
    """Status omits Cloud line when no server provisioned."""
    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Cloud:" not in result.output


def test_status_cloud_default_warning(tmp_data_dir, mock_runtime, monkeypatch):
    """Status warns when cloud is default but no server provisioned."""
    import tomli_w

    from bubble.config import CONFIG_FILE

    CONFIG_FILE.write_bytes(tomli_w.dumps({"cloud": {"default": True}}).encode())

    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Warning:" in result.output
    assert "no server provisioned" in result.output


def test_status_verbose_shows_registry(tmp_data_dir, mock_runtime, monkeypatch):
    """Verbose status shows per-bubble details from registry."""
    from bubble.lifecycle import register_bubble

    register_bubble("mathlib4-pr-123", "leanprover-community/mathlib4", pr=123)

    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status", "-v"])
    assert result.exit_code == 0
    assert "mathlib4-pr-123" in result.output
    assert "leanprover-community/mathlib4" in result.output


def test_status_runtime_unavailable(tmp_data_dir, monkeypatch):
    """Status gracefully handles runtime being unavailable."""
    from bubble.lifecycle import register_bubble

    register_bubble("test-bubble", "owner/repo")

    def broken_runtime(config, ensure_ready=True):
        raise RuntimeError("incus not found")

    monkeypatch.setattr("bubble.commands.status_cmd.get_runtime", broken_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "1 registered" in result.output


def test_status_skips_builder_containers(tmp_data_dir, mock_runtime, monkeypatch):
    """Status does not count builder containers."""
    mock_runtime._containers = {
        "base-builder": ContainerInfo(name="base-builder", state="running"),
        "real-bubble": ContainerInfo(name="real-bubble", state="running"),
    }
    _patch_runtime(monkeypatch, mock_runtime)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "1 running" in result.output
